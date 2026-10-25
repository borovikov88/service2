"""Scoped worker display evidence; no cron installation or provider requests."""
import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service import avito_runtime as runtime, avito_advance
from pool_service.avito_scheduler import scheduler_state
from pool_service.communication_models import AvitoCredential, AvitoSchedulerHeartbeat, ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess


@override_settings(SITE_URL="", ALLOWED_HOSTS=["service2.aqualine22.ru"])
class ScopedWorkerDisplayTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic scoped display")
        self.owner = User.objects.create_user("synthetic-display-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id="446312402")
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.run_id = uuid.uuid4()
        self.heartbeat = AvitoSchedulerHeartbeat.objects.create(key="avito-status-monitor", run_id=self.run_id,
            state="finished", started_at=timezone.now() - timedelta(minutes=1),
            finished_at=timezone.now(), exit_code=2)
        avito_advance.configure(self.connection, self.owner, "enable")
        self.connection.refresh_from_db()

    def begin(self):
        self.heartbeat.state = "running"
        self.heartbeat.started_at = timezone.now() - timedelta(seconds=1)
        self.heartbeat.finished_at = None
        self.heartbeat.checks = []
        self.heartbeat.save()

    def finish(self, code=2):
        self.heartbeat.refresh_from_db()
        self.heartbeat.state = "finished"
        self.heartbeat.finished_at = timezone.now()
        self.heartbeat.exit_code = code
        self.heartbeat.save()

    def display(self):
        self.connection.refresh_from_db()
        return avito_advance.display_state(self.connection, self.owner)["scheduler"]

    def render(self, state):
        return render_to_string("pool_service/avito/_advance.html", {
            "advance_monitor_state": state,
            "selected": SimpleNamespace(connection=self.connection, has_credentials=True),
            "workspace": {"advance": {"data": None}}, "workspace_next_url": "",
        })

    def test_other_connection_failure_does_not_become_services_failure_or_write_settings(self):
        other = ChannelConnection.objects.create(pk=1, channel=self.connection.channel, external_id="other-account")
        before = copy.deepcopy(other.settings)
        self.begin()
        runtime.record(1, "advance", "failed", run_id=str(self.run_id))
        self.finish()
        result = self.display()
        self.assertFalse(result["display_failed"])
        self.assertEqual("not_confirmed", result["display_state"])
        self.assertIn("Подключение №4 · Аванс", result["detail"])
        self.assertNotIn("завершилась ошибкой", result["detail"])
        self.assertNotIn("часть проверок", result["detail"])
        other.refresh_from_db()
        self.assertEqual(before, other.settings)

    def test_current_failure_has_exact_connection_component_safe_code_and_barnaul_time(self):
        self.connection.settings[avito_advance.KEY].update(last_error_code="provider_http_403")
        self.connection.save(update_fields=["settings"])
        self.begin()
        runtime.record(4, "advance", "failed", run_id=str(self.run_id))
        self.finish()
        result = self.display()
        self.heartbeat.refresh_from_db()
        stamp = runtime.when(self.heartbeat.checks[0]["checked_at"])
        from zoneinfo import ZoneInfo
        self.assertTrue(result["display_failed"])
        self.assertEqual("failed", result["display_state"])
        self.assertIn("Подключение №4 · Аванс", result["detail"])
        self.assertIn("provider_http_403", result["detail"])
        self.assertIn(stamp.astimezone(ZoneInfo("Asia/Barnaul")).strftime("%d.%m.%Y %H:%M:%S"), result["detail"])
        self.assertIn("Барнаул", result["detail"])
        # Presentation does not loosen the existing activation readiness policy.
        self.assertTrue(result["ready"])
        self.assertFalse(scheduler_state()["ready"])

    def test_current_success_is_separate_from_unrelated_failed_component(self):
        self.begin()
        runtime.record(4, "advance", "success", run_id=str(self.run_id))
        runtime.record(4, "autoload_report", "failed", run_id=str(self.run_id))
        self.finish()
        result = self.display()
        self.assertFalse(result["display_failed"])
        self.assertEqual("success", result["display_state"])
        self.assertIn("Успешная проверка подтверждена", result["detail"])
        self.assertNotIn("ошибкой", result["detail"])

    def test_due_disabled_and_skipped_never_become_worker_failure(self):
        self.connection.settings[avito_advance.KEY].update(
            last_success_at=(timezone.now() - timedelta(hours=1)).isoformat(),
            next_due_at=(timezone.now() - timedelta(minutes=1)).isoformat())
        self.connection.save(update_fields=["settings"])
        self.assertEqual("due", self.display()["display_state"])
        self.assertFalse(self.display()["display_failed"])
        self.begin()
        runtime.record(4, "advance", "skipped", run_id=str(self.run_id))
        self.finish()
        self.assertEqual("skipped", self.display()["display_state"])
        self.assertFalse(self.display()["display_failed"])
        avito_advance.configure(self.connection, self.owner, "disable")
        self.assertEqual("disabled", self.display()["display_state"])
        self.assertFalse(self.display()["display_failed"])

    def test_legacy_saved_error_without_run_evidence_is_not_a_current_worker_failure(self):
        self.connection.settings[avito_advance.KEY].update(
            last_error_code="provider_http_403",
            last_checked_at=(timezone.now() - timedelta(days=2)).isoformat())
        self.connection.save(update_fields=["settings"])
        self.assertEqual("not_confirmed", self.display()["display_state"])
        self.assertFalse(self.display()["display_failed"])

    def test_old_failed_run_and_binding_drift_do_not_prove_current_failure(self):
        self.begin()
        runtime.record(4, "advance", "failed", run_id=str(self.run_id))
        self.finish()
        self.heartbeat.started_at -= timedelta(hours=2)
        self.heartbeat.finished_at -= timedelta(hours=2)
        self.heartbeat.checks[0]["checked_at"] = self.heartbeat.finished_at.isoformat()
        self.heartbeat.save()
        self.assertFalse(self.display()["display_failed"])
        self.assertEqual("not_confirmed", self.display()["display_state"])
        self.connection.external_id = "different-account"
        self.connection.save(update_fields=["external_id"])
        self.assertFalse(self.display()["display_failed"])
        self.assertEqual("blocked", self.display()["display_state"])

    def test_advance_template_shows_actual_failure_even_when_worker_is_running_partially(self):
        self.begin()
        runtime.record(4, "advance", "failed", run_id=str(self.run_id))
        self.finish()
        self.connection.refresh_from_db()
        state = avito_advance.display_state(self.connection, self.owner)
        html = self.render(state)
        self.assertIn(state["scheduler"]["detail"], html)
        self.assertIn("text-warning", html)
        self.assertNotIn("Последний запуск обработчика завершился ошибкой", html)

    def test_tariff_and_status_monitor_use_scoped_display_and_do_not_change_gates(self):
        from pool_service import avito_tariff_reminder, avito_status_monitor
        tariff = avito_tariff_reminder.display_state(self.connection, self.owner)
        status = avito_status_monitor.display_state(self.connection, self.owner)
        self.assertEqual("disabled", tariff["scheduler"]["display_state"])
        self.assertEqual("disabled", status["scheduler"]["display_state"])
        self.assertIn("Подключение №4 · Срок тарифа", tariff["scheduler"]["detail"])
        self.assertIn("Подключение №4 · Статусы объявлений", status["scheduler"]["detail"])
        self.assertEqual(scheduler_state()["ready"], status["scheduler"]["ready"])
        self.assertEqual(scheduler_state()["worker_ready"], tariff["scheduler"]["ready"])

    def test_untrusted_diagnostic_text_never_enters_scoped_message(self):
        self.begin()
        runtime.record(4, "advance", "failed", run_id=str(self.run_id))
        self.finish()
        self.heartbeat.checks[0]["code"] = "SECRET=https://private.invalid"
        self.heartbeat.save(update_fields=["checks"])
        result = self.display()
        self.assertIn("check_error_unclassified", result["detail"])
        self.assertNotIn("SECRET", result["detail"])
        self.assertNotIn("private.invalid", result["detail"])

    def test_scoped_display_reads_only_the_selected_component(self):
        with patch.object(runtime, "_saved", wraps=runtime._saved) as saved:
            self.display()
        saved.assert_called_once()
        self.assertEqual("advance", saved.call_args.args[1])

    def test_admin_template_attributes_selected_check_and_keeps_non_failure_states_neutral(self):
        row = {"check": "advance", "label": "Аванс", "state": "due", "code": "provider_http_403",
            "last_worker_verified": True, "last_worker_outcome": "skipped",
            "last_worker_code": "workspace_busy", "last_worker_checked_at": timezone.now().isoformat()}
        account = {"connection": self.connection, "has_credentials": False,
            "webhook_status": "connected", "runtime_checks": [row]}
        html = render_to_string("pool_service/avito/_diagnostics.html", {"accounts": [account]})
        self.assertIn("Подключение №4 · ID Авито: 446312402", html)
        self.assertIn("Подключение №4 · Аванс", html)
        self.assertIn("Ожидает планового запуска", html)
        self.assertIn("В этом запуске проверка не выполнялась", html)
        self.assertIn('<span class="text-muted">provider_http_403</span>', html)
        self.assertIn('data-avito-time', html)
        self.assertNotIn("Ошибка проверки", html)
        for state, label in (("disabled", "Выключено / не настроено"),
                             ("pending", "Результат ещё не подтверждён")):
            row.update(state=state, last_worker_verified=False)
            html = render_to_string("pool_service/avito/_diagnostics.html", {"accounts": [account]})
            self.assertIn(label, html)
            self.assertNotIn("Ошибка проверки", html)
