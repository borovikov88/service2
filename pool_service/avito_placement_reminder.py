"""Local calendar reminders from saved owned Services observations, never provider writes."""
import time
import uuid
from datetime import timedelta, timezone as datetime_timezone

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .avito_deferred_messages import daylight
from .avito_feed_models import AvitoServicesFeed
from .avito_placement_continuity import ZONE, aware_time, continuity_context, scope_matches
from .avito_status_monitor import _scope_error
from .communication_models import AvitoStatusMonitor, ChannelConnection
from .models import Notification
from .services.notifications import notify_users

KEY = "avito_placement_reminder_v1"
MAX_AGE = timedelta(hours=24)
REFRESH_AFTER = timedelta(hours=12)
# Five full supported inventories. Retain known cycles rather than evicting
# delivered stages; at capacity, new identities fail closed until investigated.
MAX_TRACKED_LISTINGS = 1000


def _binding(connection):
    if (connection is None or connection.pk != 4 or connection.external_id != "446312402"
            or connection.channel.kind != "avito"):
        return None
    return {"account_id": str(connection.external_id), "channel_id": connection.channel_id,
            "organization_id": connection.channel.organization_id}


def capture_binding():
    """Pin the target before refresh I/O, including the command's exception path."""
    return _binding(ChannelConnection.objects.select_related("channel").filter(pk=4).first())


def _refresh_owned(deadline, *, binding):
    """Reuse the existing verifier, outside locks and within the worker budget."""
    from .avito_services_feed import verify_owned_observations
    connection = ChannelConnection.objects.select_related("channel").filter(pk=4).first()
    if binding is None or _binding(connection) != binding or not daylight():
        return
    monitor = AvitoStatusMonitor.objects.select_related("recipient", "organization").filter(
        connection=connection, enabled=True).first()
    feed = AvitoServicesFeed.objects.filter(connection=connection).first()
    if not scope_matches(connection, feed) or monitor is None or _scope_error(monitor, connection):
        return
    if feed.last_checked_at and timedelta(0) <= timezone.now() - feed.last_checked_at < REFRESH_AFTER:
        return
    # verify_inventory already bounds its work to 45s and uses the shared detail
    # throttle. Keep a margin, preserving the status-scan reservation of the worker.
    if deadline - time.monotonic() < 60:
        return
    generation, recipient_id = monitor.generation, monitor.recipient_id

    def guard(current, candidate):
        current_monitor = AvitoStatusMonitor.objects.select_related("recipient", "organization").filter(
            connection=current, enabled=True, generation=generation, recipient_id=recipient_id).first()
        return bool(_binding(current) == binding and scope_matches(current, candidate) and current_monitor
                    and not _scope_error(current_monitor, current))

    verify_owned_observations(connection, monitor.recipient, expected_revision=feed.revision,
                              expected_guard=guard)


def _stage(days):
    # On the source date, catch up the missed one-day stage without claiming
    # expiry. Past dates require a fresh operator decision, not a due notification.
    return next((stage for stage in (1, 3, 7) if 0 <= days <= stage), None)


def scan_saved(*, deadline=None, observer=None, expected_binding=None):
    """One bounded local check in the existing worker; no separate schedule/collector."""
    binding = expected_binding if expected_binding is not None else capture_binding()
    if binding is None:
        return "skipped", 0
    refresh_failed = False
    if deadline is not None:
        try:
            _refresh_owned(deadline, binding=binding)
        except Exception:
            # Refresh and delivery are independent. Revalidate saved evidence
            # below, preserving the 24h limit, and report the partial tick failure
            # without persisting/logging exception strings or provider payloads.
            refresh_failed = True
    outcome, count, reason, binding = _scan_saved(refresh_failed=refresh_failed, binding=binding)
    if refresh_failed:
        outcome, reason = "failed", "placement_refresh_failed"
    if observer is not None and binding is not None:
        observer(4, "placement_reminder", outcome, reason=reason,
                 notification_count=count, binding=binding)
    return outcome, count


def _scan_saved(*, refresh_failed, binding):
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related(
            "channel__organization").filter(pk=4).first()
        if binding is None or _binding(connection) != binding:
            # A new channel in the same organization must not adopt a refresh
            # failure or fallback notifications from the original channel.
            return "skipped", 0, "binding_changed", None

        def result(outcome, count, reason=""):
            return outcome, count, reason, binding

        monitor = AvitoStatusMonitor.objects.select_for_update().select_related(
            "recipient", "organization").filter(connection=connection).first()
        feed = AvitoServicesFeed.objects.select_for_update().filter(connection=connection).first()
        if not scope_matches(connection, feed):
            return result("skipped", 0, "binding_changed")
        if monitor is None:
            return result("skipped", 0, "not_configured")
        if not monitor.enabled:
            return result("skipped", 0, "disabled")
        if _scope_error(monitor, connection):
            return result("skipped", 0, "placement_recipient_unavailable")
        now = timezone.now()
        # Quiet hours never consume a stage; next native tick after 08:00 catches up.
        if not daylight(now):
            return result("skipped", 0, "quiet_hours")
        if feed.last_checked_at is None or not timedelta(0) <= now - feed.last_checked_at <= MAX_AGE:
            return result("skipped", 0, "placement_stale")
        if not isinstance(feed.latest_observations, list) or not isinstance(feed.draft, list):
            return result("skipped", 0, "placement_data_invalid")
        rows = []
        for sheet in feed.draft:
            if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
                return result("skipped", 0, "placement_data_invalid")
            rows.extend(sheet["rows"])
        if (not 1 <= len(rows) <= 200 or any(not isinstance(row, dict)
                or not isinstance(row.get("Id"), str) or not row["Id"]
                or not isinstance(row.get("AvitoId"), str) or not row["AvitoId"].isdigit()
                for row in rows)):
            return result("skipped", 0, "placement_data_invalid")
        observations = feed.latest_observations
        if (len(observations) > 200 or any(not isinstance(row, dict)
                or not isinstance(row.get("avito_id"), str) for row in observations)):
            return result("skipped", 0, "placement_data_invalid")
        owned = {row["avito_id"]: row for row in observations}
        if len(owned) != len(observations) or len({row["AvitoId"] for row in rows}) != len(rows):
            return result("skipped", 0, "placement_data_invalid")
        withdrawn = set(feed.listing_intents.filter(account_id=connection.external_id,
            organization_id=connection.channel.organization_id, owner_withdrawn=True
        ).values_list("avito_id", flat=True))
        projected = continuity_context(connection, feed, [dict(row,
            observation=owned.get(row["AvitoId"]),
            archive_control={"owner_withdrawn": row["AvitoId"] in withdrawn}) for row in rows], now=now)
        # Preserve the existing explicit self-subscription and recipient rights.
        recipients = [monitor.recipient]
        # The existing status snapshot may already know that a saved owned active
        # observation is obsolete. It must never revive that listing for a reminder.
        statuses = {row.item_id: row for row in monitor.listings.all()}
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        saved = metadata.get(KEY)
        saved = saved if isinstance(saved, dict) and saved.get("binding") == binding else {}
        cycles = saved.get("listings") if isinstance(saved.get("listings"), dict) else {}
        if len(cycles) > MAX_TRACKED_LISTINGS:
            return result("failed", 0, "placement_capacity_exhausted")
        # Absence from one draft is not evidence of a new placement cycle. Keep
        # its generation/stages so restoring the same identity cannot resend.
        next_cycles, count, capacity_exhausted = dict(cycles), 0, False
        stale, eligible, preferences_disabled, quiet = False, False, False, False
        for row in projected["rows"]:
            identifier = row["avito_id"]
            if identifier not in next_cycles and len(next_cycles) >= MAX_TRACKED_LISTINGS:
                capacity_exhausted = True
                continue
            previous = cycles.get(identifier)
            previous = dict(previous) if isinstance(previous, dict) else {}
            next_cycles[identifier] = previous
            checked = row["checked_at"]
            if checked is None or not timedelta(0) <= now - checked <= MAX_AGE:
                stale = True
                continue
            if row["status"] != "active":
                previous["last_status"] = row["status"]
                continue
            status = statuses.get(identifier)
            current_status = (status.status if status and status.last_seen_at >= checked else row["status"])
            if current_status != "active":
                previous["last_status"] = current_status
                continue
            if row["state"] == "owner_withdrawn" or row["days_remaining"] is None:
                continue
            observation = owned[identifier]
            # Exact instants retain their explicit offset; calendar dates retain
            # only the source date. Neither receives an invented timezone/time.
            term = (row["finish_time"].astimezone(datetime_timezone.utc).isoformat()
                    if row["finish_time"] else row["source_finish_date"].isoformat())
            signature = [observation["id"], term]
            reactivation = observation.get("reactivation")
            if (isinstance(reactivation, dict) and reactivation.get("confirmed") is True
                    and reactivation.get("source") == "same_identity_provider_status_and_term"
                    and aware_time(reactivation.get("confirmed_at")) is not None):
                signature.append(reactivation["confirmed_at"])
            if (previous.get("signature") != signature
                    or not isinstance(previous.get("generation"), str)):
                previous = {"signature": signature, "generation": uuid.uuid4().hex, "delivered": {}}
                next_cycles[identifier] = previous
            previous["last_status"] = "active"
            stage = _stage(row["days_remaining"])
            if stage is None:
                continue
            eligible = True
            delivered = previous.get("delivered")
            delivered = dict(delivered) if isinstance(delivered, dict) else {}
            for recipient in recipients:
                if not daylight():
                    quiet = True
                    break
                last = delivered.get(str(recipient.pk))
                if type(last) is int and last <= stage:
                    continue
                dedupe = f"avito-placement:4:{identifier}:{previous['generation']}:{stage}"
                created = notify_users([recipient], organization=connection.channel.organization,
                    kind="communication", level="warning", send_push=False,
                    title=f"Авито Услуги: близкая дата размещения (порог {stage} дн.)",
                    message=f"Объявление ID {identifier}: дата срока {row['source_finish_date'] or row['finish_time'].astimezone(ZONE).date():%d.%m.%Y}; "
                        f"осталось {row['days_remaining']} календарных дн. "
                        + ("Дата источника без подтверждённого часового пояса и точного момента. "
                           if row["source_finish_date"] else "Дата показана по Барнаулу. ")
                        + "Проверьте существующую карточку и условия в Авито. Автоматическое повторное размещение не выполняется.",
                    action_url=reverse("avito_dashboard") + "?account=4", dedupe_key=dedupe)
                # Opt-out does not consume this recipient's stage; retry can recover.
                if created or Notification.objects.filter(user=recipient, organization=connection.channel.organization,
                        kind="communication", dedupe_key=dedupe).exists():
                    delivered[str(recipient.pk)] = stage
                else:
                    preferences_disabled = True
                count += len(created)
            previous["delivered"] = delivered
        metadata[KEY] = {"binding": binding, "listings": next_cycles,
                         "last_refresh_failed": refresh_failed, "capacity_exhausted": capacity_exhausted,
                         "last_checked_at": now.isoformat()}
        connection.settings = metadata
        connection.save(update_fields=["settings"])
        reason = ("placement_capacity_exhausted" if capacity_exhausted else "quiet_hours" if quiet
                  else "notification_preferences_disabled" if preferences_disabled
                  else "placement_stale" if stale else "" if count else
                  "placement_already_recorded" if eligible else "placement_no_eligible")
        return result("failed" if capacity_exhausted else "success", count, reason)
