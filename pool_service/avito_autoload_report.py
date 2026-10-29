"""Bounded read-only Avito Autoload v4 reports; aliases are checked twice."""
import csv
import hashlib
import io
import json
import re
import time
from collections import Counter
from urllib.parse import urlencode, urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.communication_avito import AvitoError

KINDS = ("current", "last_successful")
PAGE_SIZE = 50
MAX_ITEMS = 5000
MAX_SECONDS = 55
MAX_BYTES = 2 * 1024 * 1024
STATUSES = {
    "active": "Активно", "old": "Истёк срок", "blocked": "Заблокировано",
    "rejected": "Отклонено", "archived": "В архиве", "removed": "Удалено навсегда",
}
UPLOAD_STATUSES = {"processing", "success", "success_warning", "error", None}
MESSAGE_TYPES = {"error", "warning", "alarm", "info"}
ERRORS = {
    "autoload_report_invalid": "Авито вернул неподдерживаемую структуру отчёта. Полнота данных не подтверждена.",
    "autoload_report_changed": "Загрузка изменилась во время чтения. Согласованный снимок не сохранён; повторите проверку.",
    "autoload_report_limit": "Отчёт не получен целиком в пределах размера, количества или времени. Предыдущий снимок сохранён, если был.",
    "autoload_report_pagination": "Пагинация отчёта неполная или противоречивая. Полнота данных не подтверждена.",
    "autoload_report_duplicate_rows": "В отчёте Авито повторяются строки одного объявления. Полнота данных и счётчики не подтверждены; повторите проверку.",
}


CONSISTENCY_GUARDS = {"queried_rows_changed", "shop_xml_snapshot_changed",
                      "profile_snapshot_changed", "summary_snapshot_changed"}

ROW_DIFFERENCE_LABELS = {
    "missing_identity": "нет совпадающей пары XML ID / ID Авито",
    "avito_status": "статус объявления", "avito_date_end": "срок размещения",
    "feed_name": "название источника", "section": "раздел отчёта",
    "messages": "сообщения Авито", "message_order_only": "только порядок сообщений",
    "message_time_only": "только время сообщений", "other_fields": "прочие поля строки",
}


def safe_row_differences(value):
    """Fixed field names and bounded counts only; never provider values or IDs."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in ROW_DIFFERENCE_LABELS
            if type(value.get(key)) is int and 0 < value[key] <= PAGE_SIZE}


def _message_counts(values, *, omit_time=False):
    return Counter(json.dumps({key: value for key, value in entry.items()
                               if not (omit_time and key == "updated_at")}, sort_keys=True)
                   for entry in values)


def _same_report_row(probe, other):
    """Only message ordering is immaterial; retain every value and multiplicity."""
    return (other is not None
            and {key: value for key, value in probe.items() if key != "messages"} == {
                key: value for key, value in other.items() if key != "messages"}
            and _message_counts(probe["messages"]) == _message_counts(other["messages"]))


def row_difference_counts(probe_rows, by_identity):
    counts = Counter()
    for row in probe_rows:
        other = by_identity.get((row["ad_id"], row["avito_id"]))
        if other is None:
            counts["missing_identity"] += 1
            continue
        for key in ("avito_status", "avito_date_end", "feed_name", "section", "messages"):
            if row[key] != other[key]:
                counts[key] += 1
        # Order-only rows can accompany a genuine mismatch elsewhere in the probe.
        if row["messages"] != other["messages"]:
            if _message_counts(row["messages"]) == _message_counts(other["messages"]):
                counts["message_order_only"] += 1
            elif _message_counts(row["messages"], omit_time=True) == _message_counts(other["messages"], omit_time=True):
                counts["message_time_only"] += 1
        known = {"ad_id", "avito_id", "avito_status", "avito_date_end", "feed_name", "section", "messages"}
        if {key: value for key, value in row.items() if key not in known} != {
                key: value for key, value in other.items() if key not in known}:
            counts["other_fields"] += 1
    return safe_row_differences(counts)


def consistency_guard(value):
    return value if isinstance(value, str) and value in CONSISTENCY_GUARDS else None


class ReportChanged(AvitoError):
    """Fixed guard identity only; the original top-level rejection code remains."""
    def __init__(self, guard, *, row_differences=None):
        if consistency_guard(guard) is None:
            raise ValueError("invalid report consistency guard")
        super().__init__("autoload_report_changed")
        self.consistency_guard = guard
        self.row_differences = safe_row_differences(row_differences) if guard == "queried_rows_changed" else {}


def failure_detail(error):
    detail = ERRORS[str(error)]
    if (str(error) == "autoload_report_changed"
            and getattr(error, "consistency_guard", None) == "queried_rows_changed"):
        detail += " Строки выборки по XML ID отличаются от проверочной страницы."
        counts = safe_row_differences(getattr(error, "row_differences", None))
        if counts:
            detail += " Различия (число строк): " + "; ".join(
                f"{ROW_DIFFERENCE_LABELS[key]} — {count}" for key, count in counts.items()) + "."
    return detail


def _invalid():
    raise AvitoError("autoload_report_invalid")


def _text(value, limit=200):
    if not isinstance(value, str) or len(value) > 50000:
        _invalid()
    text = " ".join(value.split())
    text = "".join(char for char in text if char >= " " and char != "\x7f")
    # Provider messages can echo quoted JSON credentials, including spaced values.
    # Discard the whole marked text rather than guessing the value's delimiter.
    if re.search(r"(?i)\b(access_token|client_secret|refresh_token|password)\b", text):
        return "[текст с учётными данными скрыт]"
    # Descriptions can echo rejected contact fields or signed URLs.
    text = re.sub(r"https?://\S+", "[ссылка скрыта]", text, flags=re.I)
    text = re.sub(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[email скрыт]", text)
    text = re.sub(r"\+?[78](?:[\s()-]*[0-9]){10}(?![0-9])", "[телефон скрыт]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [скрыто]", text)
    text = re.sub(r"(?i)\b(access_token|client_secret|refresh_token|password)\s*[:=]\s*\S+", r"\1=[скрыто]", text)
    return text[:limit]


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_ITEMS:
        _invalid()
    return value


def _date(value, *, nullable=False):
    if nullable and value is None:
        return ""
    if not isinstance(value, str) or len(value) > 50:
        _invalid()
    try:
        parsed = parse_datetime(value)
    except ValueError:
        _invalid()
    if parsed is None or timezone.is_naive(parsed):
        _invalid()
    return parsed.isoformat()


def _tree(node, depth=0):
    if not isinstance(node, dict) or depth > 2:
        _invalid()
    result = {"slug": _text(node.get("slug"), 100), "title": _text(node.get("title"), 200),
              "count": _count(node.get("count")), "sections": []}
    if depth < 2:
        children = node.get("sections")
        if not isinstance(children, list) or len(children) > 100:
            _invalid()
        result["sections"] = [_tree(child, depth + 1) for child in children]
    return result


def shop_feed_source(feeds):
    # Only publish a fixed source kind. Never retain names, URLs or tokens.
    if not isinstance(feeds, list) or not 1 <= len(feeds) <= 50:
        return "other_or_unknown"
    kinds = []
    for feed in feeds:
        value = feed.get("url") if isinstance(feed, dict) else None
        if not isinstance(value, str) or len(value) > 2000:
            return "other_or_unknown"
        try:
            part = urlsplit(value)
            if (part.scheme not in ("http", "https") or part.hostname != "shop.aqualine22.ru"
                    or part.username is not None or part.password is not None
                    or part.port not in (None, 80 if part.scheme == "http" else 443)
                    or part.query or part.fragment):
                return "other_or_unknown"
        except ValueError:
            return "other_or_unknown"
        kinds.append({"/avito/avitofeed.xml": "shop_aggregate_file",
                      "/avito/avitofeed_43.xml": "shop_profile_43_file"}.get(part.path, "other_or_unknown"))
    return kinds[0] if len(set(kinds)) == 1 else "other_or_unknown"


def feed_source_diagnostics(feeds):
    """Fixed structural facts only; URLs, names and parameter values stay private."""
    if not isinstance(feeds, list) or len(feeds) > 50:
        _invalid()
    result = []
    for feed in feeds:
        value = feed.get("url") if isinstance(feed, dict) else feed if isinstance(feed, str) else None
        shape = "object_url" if isinstance(feed, dict) and isinstance(value, str) else "string" if isinstance(feed, str) else "unsupported"
        row = {"shape": shape, "scheme": "unknown", "host_kind": "unknown",
               "path_kind": "unknown", "query_present": False,
               "fragment_present": False, "credentials_present": False}
        if isinstance(value, str) and len(value) <= 2000:
            try:
                part = urlsplit(value)
                known_host = {"shop.aqualine22.ru": "shop", "www.shop.aqualine22.ru": "shop_www",
                              "shop.borovidz.beget.tech": "shop_legacy", "service2.aqualine22.ru": "service2",
                              "aqualine22.ru": "info_site"}.get(part.hostname)
                if known_host is None and part.hostname and (
                        part.hostname == "avito.ru" or part.hostname.endswith(".avito.ru")
                        or part.hostname == "avito.st" or part.hostname.endswith(".avito.st")):
                    known_host = "avito"
                row.update(scheme=part.scheme if part.scheme in ("http", "https") else "unknown",
                           host_kind=known_host or "other",
                           path_kind={"/avito/avitofeed.xml": "aggregate_file",
                                      "/avito/avitofeed_43.xml": "profile_43_file",
                                      "/index.php": "index_route", "/": "root"}.get(part.path, "other"),
                           query_present=bool(part.query), fragment_present=bool(part.fragment),
                           credentials_present=part.username is not None or part.password is not None)
            except ValueError:
                pass
        result.append(row)
    return result


def source_url_sha256(feeds):
    """Backend-only exact source fingerprints; raw URLs remain provider-local."""
    if not isinstance(feeds, list) or len(feeds) > 50:
        _invalid()
    result = []
    for feed in feeds:
        value = feed.get("url") if isinstance(feed, dict) else feed if isinstance(feed, str) else None
        if not isinstance(value, str) or not 1 <= len(value) <= 2000:
            continue
        try:
            part = urlsplit(value)
            valid = (part.scheme == "https" and part.hostname and part.username is None
                     and part.password is None and part.port in (None, 443)
                     and not part.fragment and not any(c.isspace() for c in value))
        except ValueError:
            valid = False
        if valid:
            result.append(hashlib.sha256(value.encode("utf-8")).hexdigest())
    return result


def summary_data(raw):
    if not isinstance(raw, dict):
        _invalid()
    identifier = raw.get("upload_id")
    status = raw.get("status")
    if type(identifier) is not int or not 0 < identifier < 10**32 or (status is not None and not isinstance(status, str)) or status not in UPLOAD_STATUSES:
        _invalid()
    events = raw.get("events")
    if not isinstance(events, list) or len(events) > 50 or not isinstance(raw.get("feed_urls"), list):
        _invalid()
    normalized_events = []
    for event in events:
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            _invalid()
        normalized_events.append({"code": event["code"], "type": _text(event.get("type"), 50),
                                  "description": _text(event.get("description"), 3000)})
    return {"id": str(identifier), "status": status or "", "started_at": _date(raw.get("started_at"), nullable=True),
            "stats": _tree(raw.get("stats")), "events": normalized_events,
            "source_url_sha256": source_url_sha256(raw["feed_urls"]),
            "shop_feed_source": shop_feed_source(raw["feed_urls"]),
            "shop_feed_diagnostics": feed_source_diagnostics(raw["feed_urls"])}


def summary_for_workspace(raw):
    data = summary_data(raw)
    counters = []
    def visit(node, parents=()):
        label = " / ".join((*parents, node["title"]))
        counters.append({"label": label, "value": node["count"]})
        for child in node["sections"]:
            visit(child, (*parents, node["title"]))
    visit(data["stats"])
    return {key: data[key] for key in ("id", "status", "started_at")} | {"finished_at": "", "counters": counters}


def _item(raw):
    if not isinstance(raw, dict):
        _invalid()
    identifier = raw.get("ad_id")
    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 100
            or identifier != identifier.strip() or any(char < " " or char == "\x7f" for char in identifier)):
        _invalid()
    avito_id, status = raw.get("avito_id"), raw.get("avito_status")
    if avito_id is not None and (type(avito_id) is not int or not 0 <= avito_id < 10**32):
        _invalid()
    if status is not None and (not isinstance(status, str) or status not in STATUSES):
        _invalid()
    section = raw.get("section")
    if not isinstance(section, dict):
        _invalid()
    messages = raw.get("messages")
    if not isinstance(messages, list) or len(messages) > 50:
        _invalid()
    normalized = []
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get("type"), str)
                or message["type"] not in MESSAGE_TYPES or type(message.get("code")) is not int):
            _invalid()
        description = message.get("description")
        entry = {
            "type": message["type"], "code": message["code"], "title": _text(message.get("title"), 200),
            "description": _text(description, 3000), "updated_at": _date(message.get("updated_at")),
            "truncated": message.get("truncated") is True or len(description) > 3000,
        }
        from .avito_report_messages import message_diagnostics
        entry["diagnostics"] = message_diagnostics(message, entry)
        normalized.append(entry)
    return {"ad_id": identifier, "avito_id": avito_id, "avito_status": status,
            "avito_date_end": _date(raw.get("avito_date_end"), nullable=True) or None,
            "feed_name": _text(raw["feed_name"], 200) if raw.get("feed_name") is not None else "",
            "status_label": STATUSES.get(status, "Не передан"),
            "section": {"slug": _text(section.get("slug"), 100), "title": _text(section.get("title"), 200)},
            "messages": normalized}


def _page(raw, page, expected=None):
    if not isinstance(raw, dict) or not isinstance(raw.get("meta"), dict) or not isinstance(raw.get("items"), list):
        raise AvitoError("autoload_report_pagination")
    meta, rows = raw["meta"], raw["items"]
    if any(type(meta.get(key)) is not int for key in ("page", "pages", "perPage", "total")):
        raise AvitoError("autoload_report_pagination")
    total, pages = meta["total"], meta["pages"]
    if (meta["page"] != page or meta["perPage"] != PAGE_SIZE or not 0 <= total <= MAX_ITEMS
            or (total == 0 and pages not in (0, 1))
            or (total > 0 and pages != (total + PAGE_SIZE - 1) // PAGE_SIZE)
            or (expected is not None and (total, pages) != expected)
            or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))):
        raise AvitoError("autoload_report_pagination")
    return [_item(row) for row in rows], (total, pages)


def require_unique_rows(rows):
    """Repeated XML/Avito identity pairs cannot prove a complete item scan."""
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        _invalid()
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            _invalid()
        identifier, avito_id = row.get("ad_id"), row.get("avito_id")
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 100
                or identifier != identifier.strip()
                or any(char < " " or char == "\x7f" for char in identifier)
                or (avito_id is not None and (type(avito_id) is not int or not 0 <= avito_id < 10**32))):
            _invalid()
        identity = (identifier, avito_id)
        if identity in seen:
            raise AvitoError("autoload_report_duplicate_rows")
        seen.add(identity)


class ReportDuplicateRows(AvitoError):
    """Validated report metadata only; rows remain unusable for reconciliation."""
    def __init__(self, diagnostics):
        super().__init__("autoload_report_duplicate_rows")
        self.diagnostics = diagnostics


def duplicate_row_diagnostics(rows, upload, pages):
    # Called only after all pages and both alias summaries were validated.
    # Compare normalized rows locally; never expose identities, messages or feeds.
    seen, samples = {}, []
    same_page = cross_page = identical = different = 0
    for offset, row in enumerate(rows):
        position, page = offset + 1, offset // PAGE_SIZE + 1
        identity = (row["ad_id"], row["avito_id"])
        if identity not in seen:
            seen[identity] = (row, position, {page})
            continue
        first, first_position, seen_pages = seen[identity]
        same_page += page in seen_pages
        cross_page += page not in seen_pages
        equal = row == first
        identical += equal
        different += not equal
        if len(samples) < 5:
            samples.append({"first_row": first_position, "repeated_row": position,
                            "first_page": (first_position - 1) // PAGE_SIZE + 1,
                            "repeated_page": page, "identical_payload": equal})
        seen_pages.add(page)
    return {"complete": False, "upload_id": upload["id"], "upload_status": upload["status"],
            "started_at": upload["started_at"] or None, "read_rows": len(rows),
            "pages": pages, "unique_identity_pairs": len(seen),
            "duplicate_extra_rows": len(rows) - len(seen),
            "same_page_duplicate_rows": same_page, "cross_page_duplicate_rows": cross_page,
            "identical_duplicate_rows": identical, "different_duplicate_rows": different,
            "samples": samples}


def _shop_snapshot(deadline):
    from pool_service import avito_source_audit
    from scripts.avito_readonly_receipt import xml_id
    if time.monotonic() >= deadline:
        raise AvitoError("autoload_report_limit")
    payload = avito_source_audit._read_xml(avito_source_audit.FEED_PATH, deadline=deadline)
    root = avito_source_audit._root(payload, "Ads")
    identifiers = []
    for ad in root:
        nodes = ad.findall("Id")
        if len(nodes) != 1 or len(nodes[0]) or not isinstance(nodes[0].text, str):
            return None
        value = nodes[0].text
        try:
            xml_id(value)
        except ValueError:
            return None
        identifiers.append(value)
    if not identifiers or len(set(identifiers)) != len(identifiers):
        return None
    return identifiers, hashlib.sha256(payload).hexdigest()


def _query_shop_rows(get, base, snapshot, probe_rows, total, deadline):
    # Replace pagination only when every current shop ID can be proved present.
    # Filter results are never deduplicated or combined with the unfiltered probe.
    identifiers, digest = snapshot
    rows, stored_bytes = [], 0
    for offset in range(0, len(identifiers), PAGE_SIZE):
        requested = identifiers[offset:offset + PAGE_SIZE]
        query = urlencode({"perPage": PAGE_SIZE, "page": 1, "query": ",".join(requested)})
        batch, meta = _page(get(f"{base}/items?{query}"), 1)
        if meta != (len(requested), 1) or {row["ad_id"] for row in batch} != set(requested):
            raise AvitoError("autoload_report_pagination")
        require_unique_rows(batch)
        stored_bytes += sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in batch)
        if stored_bytes > MAX_BYTES:
            raise AvitoError("autoload_report_limit")
        rows.extend(batch)
    require_unique_rows(rows)
    if len(rows) != total or {row["ad_id"] for row in rows} != set(identifiers):
        raise AvitoError("autoload_report_pagination")
    by_identity = {(row["ad_id"], row["avito_id"]): row for row in rows}
    if any(not _same_report_row(row, by_identity.get((row["ad_id"], row["avito_id"]))) for row in probe_rows):
        raise ReportChanged("queried_rows_changed", row_differences=row_difference_counts(probe_rows, by_identity))
    after = _shop_snapshot(deadline)
    if after is None or after[1] != digest:
        raise ReportChanged("shop_xml_snapshot_changed")
    return rows


def fetch_report(token, account_id, *, kind="last_successful", include_input_proof=False, input_proof_connection=None,
                 allow_shop_recovery=True):
    from pool_service import avito_workspace
    if kind not in KINDS or type(allow_shop_recovery) is not bool:
        _invalid()
    if type(include_input_proof) is not bool:
        raise ValueError("include_input_proof must be boolean")
    deadline = time.monotonic() + MAX_SECONDS
    def get(path):
        while True:
            if time.monotonic() >= deadline:
                raise AvitoError("autoload_report_limit")
            try:
                avito_workspace.enforce_rate(account_id, "autoload_report", seconds=3)
                break
            except avito_workspace.AvitoCooldownError as exc:
                wait = max(0.05, (exc.retry_at - timezone.now()).total_seconds())
                if time.monotonic() + wait >= deadline:
                    raise AvitoError("autoload_report_limit") from None
                time.sleep(min(wait, 3))
        return avito_workspace._get(token, path)

    base = f"/autoload/v4/uploads/{kind}"
    before_payload = get(base)
    before = summary_data(before_payload)
    rows, expected, page, stored_bytes = [], None, 1, 0
    read_strategy = "provider_pages"
    while True:
        batch, expected = _page(get(f"{base}/items?perPage={PAGE_SIZE}&page={page}"), page, expected)
        if (allow_shop_recovery and page == 1 and kind == "last_successful"
                and before["status"] in {"success", "success_warning", "error"}
                and before["shop_feed_diagnostics"]
                and all(row["host_kind"] in {"shop", "avito", "unknown"}
                        for row in before["shop_feed_diagnostics"])):
            from pool_service.avito_profile_source import fetch_profile_source
            try:
                profile, fingerprint = fetch_profile_source(token, account_id, deadline=deadline)
                verified = (profile.get("complete") is True and profile.get("autoload_enabled") is True
                            and profile.get("shop_feed_source") == "shop_aggregate_file"
                            and profile.get("shop_feed_diagnostics")
                            and all(row["scheme"] == "https" for row in profile["shop_feed_diagnostics"]))
                snapshot = _shop_snapshot(deadline) if verified else None
            except AvitoError:
                snapshot = None
            if snapshot is not None and len(snapshot[0]) == expected[0]:
                rows = _query_shop_rows(get, base, snapshot, batch, expected[0], deadline)
                after_profile, after_fingerprint = fetch_profile_source(token, account_id, deadline=deadline)
                if after_profile != profile or after_fingerprint != fingerprint:
                    raise ReportChanged("profile_snapshot_changed")
                read_strategy = "shop_xml_id_queries"
                page = max(1, expected[1])
                break
        stored_bytes += sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in batch)
        if stored_bytes > MAX_BYTES:
            raise AvitoError("autoload_report_limit")
        rows.extend(batch)
        if page >= max(1, expected[1]):
            break
        page += 1
    after_payload = get(base)
    after = summary_data(after_payload)
    # Provider-hosted copies may rotate signed URLs without changing the upload.
    # Preserve the existing report consistency contract; exact source proof is
    # withheld if its private URL fingerprint changed during this read.
    source_consistent = before["source_url_sha256"] == after["source_url_sha256"]
    if ({key: value for key, value in before.items() if key != "source_url_sha256"}
            != {key: value for key, value in after.items() if key != "source_url_sha256"}):
        raise ReportChanged("summary_snapshot_changed")
    try:
        require_unique_rows(rows)
    except AvitoError as error:
        if str(error) != "autoload_report_duplicate_rows":
            raise
        raise ReportDuplicateRows(duplicate_row_diagnostics(rows, before, page)) from None
    counts = Counter()
    for row in rows:
        for message_type in MESSAGE_TYPES:
            if any(message["type"] == message_type for message in row["messages"]):
                counts[message_type] += 1
    result = {"version": 1, "complete": True, "kind": kind, "upload_id": before["id"],
              "upload_status": before["status"], "upload_finished": before["status"] in {"success", "success_warning", "error"},
              "started_at": before["started_at"], "stats": before["stats"], "events": before["events"],
              "source_url_sha256": before["source_url_sha256"] if source_consistent else [],
              "shop_feed_source": before["shop_feed_source"],
              "shop_feed_diagnostics": before["shop_feed_diagnostics"],
              "total": len(rows), "pages": page, "counts": dict(counts), "rows": rows,
              "read_strategy": read_strategy}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        raise AvitoError("autoload_report_limit")
    if include_input_proof:
        from .avito_input_copy import fetch_input_proof, unavailable
        # Optional proof never turns a complete valid report into a failed read.
        try:
            proof = (fetch_input_proof(after_payload, result, input_proof_connection, account_id, deadline=deadline)
                     if source_consistent else unavailable("input_copy_source_changed"))
            if proof["state"] == "verified_xml":
                try:
                    confirmation = summary_data(get(base))
                    if ({key: value for key, value in confirmation.items() if key != "source_url_sha256"}
                            != {key: value for key, value in after.items() if key != "source_url_sha256"}):
                        proof = unavailable("input_copy_report_changed")
                    elif confirmation["source_url_sha256"] != after["source_url_sha256"]:
                        proof = unavailable("input_copy_source_changed")
                except Exception:
                    proof = unavailable("input_copy_report_recheck_failed")
        except Exception:
            proof = unavailable("input_copy_check_failed")
        result["input_copy_proof"] = proof
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES + 4096:
            result["input_copy_proof"] = unavailable("input_copy_size_invalid")
    return result


def csv_text(report, snapshot_time):
    if (not isinstance(report, dict) or report.get("version") != 1 or report.get("complete") is not True
            or report.get("kind") not in KINDS or not re.fullmatch(r"[0-9]{1,32}", str(report.get("upload_id", "")))
            or not isinstance(report.get("rows"), list) or len(report["rows"]) > MAX_ITEMS):
        _invalid()
    rows = [_item(row) for row in report["rows"]]
    require_unique_rows(rows)
    def cell(value):
        text = "" if value is None else str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["XML ID", "ID Авито", "Статус", "Раздел", "Тип", "Код", "Сообщение", "Описание",
                     "Актуальность сообщения", "Текст усечён", "Загрузка", "Режим", "Статус загрузки", "Снимок Service2",
                     "Файл", "Конец оплаченного периода"])
    for row in rows:
        for message in row["messages"] or [{}]:
            values = [row["ad_id"], row["avito_id"], row["status_label"], row["section"]["title"],
                      message.get("type"), message.get("code"), message.get("title"), message.get("description"),
                      message.get("updated_at"), message.get("truncated", False), report["upload_id"], report["kind"],
                      report.get("upload_status", ""), snapshot_time, row["feed_name"], row["avito_date_end"]]
            writer.writerow([cell(value) for value in values])
    events = report.get("events", [])
    if not isinstance(events, list) or len(events) > 50:
        _invalid()
    for event in events:
        if not isinstance(event, dict) or type(event.get("code")) is not int:
            _invalid()
        writer.writerow([cell(value) for value in (
            "", "", "", "Вся загрузка", _text(event.get("type"), 50), event["code"],
            "Событие загрузки", _text(event.get("description"), 3000), "", False,
            report["upload_id"], report["kind"], report.get("upload_status", ""), snapshot_time, "", "",
        )])
    return "\ufeff" + output.getvalue()
