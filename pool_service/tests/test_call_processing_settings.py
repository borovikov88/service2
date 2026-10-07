"""Draft settings, organization/owner isolation and metadata-only preview."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Organization, OrganizationAccess
from pool_service.communication_models import PhoneCall, TelephonyConnection, CallAnalysis
from pool_service.call_processing_models import CallProcessingRule, CallPrivateNumber, CallProcessingRuleAudit
from pool_service.services import call_processing_settings as service
from pool_service.call_processing_views import _rule_rows


class CallProcessingSettingsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Call preferences test")
        self.other_org = Organization.objects.create(name="Other preferences test")
        self.owner = User.objects.create_user("preferences-owner", password="test")
        self.employee = User.objects.create_user("preferences-employee", password="test")
        self.other_owner = User.objects.create_user("preferences-other-owner", password="test")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.employee, role="manager")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.other_owner, role="owner")
        self.connection = TelephonyConnection.objects.create(organization=self.org, external_id="preferences-test")
        self.now = timezone.now()

    def save(self, **changes):
        arguments = dict(
            user=self.owner, organization=self.org, employee_id=self.employee.pk,
            mode="all_except", include_staff=True, numbers_text="", expected_revision=0,
        )
        arguments.update(changes)
        return service.save_rule(**arguments)

    def call(self, **changes):
        values = dict(
            organization=self.org, connection=self.connection, external_id="call-" + str(PhoneCall.objects.count()),
            employee=self.employee, provider_extension="101", provider_user="employee-ref",
            phone_number="+12025550101", direction="in", result="answered", duration_seconds=120,
            started_at=self.now - timedelta(minutes=30), recording_status="stored",
            recording_file="communications/test-audio.mp3",
        )
        values.update(changes)
        return PhoneCall.objects.create(**values)

    def identity_rows(self):
        return [dict(connection_id=self.connection.pk, extension="101", external_user="employee-ref", **{"employee__user_id": self.employee.pk})]

    def preview(self):
        with patch.object(service, "_identity_rows", return_value=self.identity_rows()):
            return service.preview_rules(user=self.owner, organization=self.org, now=self.now)

    def force_other_owner_into_org(self):
        access = OrganizationAccess.objects.get(
            organization=self.other_org, user=self.other_owner
        )
        OrganizationAccess.objects.filter(pk=access.pk).update(organization=self.org)
        access.refresh_from_db()
        return access

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_owner_page_is_read_only_and_discloses_preparation_state(self, _blocked):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("call_processing_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Автоматический запуск ещё выключен")
        self.assertContains(response, 'name="expected_revision"')
        self.assertFalse(CallProcessingRule.objects.exists())
        self.assertFalse(CallProcessingRuleAudit.objects.exists())
        self.assertFalse(CallAnalysis.objects.exists())

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_manager_and_admin_cannot_read_owner_preferences(self, _blocked):
        self.client.force_login(self.employee)
        for role in ("manager", "admin"):
            OrganizationAccess.objects.filter(
                organization=self.org, user=self.employee
            ).update(role=role)
            self.assertEqual(
                self.client.get(reverse("call_processing_settings")).status_code,
                403,
            )

        OrganizationAccess.objects.filter(
            organization=self.org, user=self.employee
        ).update(role="accountant")
        response = self.client.get(reverse("call_processing_settings"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("finance_dashboard"))

    def test_anonymous_requires_login_and_post_requires_csrf(self):
        self.assertEqual(self.client.get(reverse("call_processing_settings")).status_code, 302)
        client = TestClient(enforce_csrf_checks=True)
        client.force_login(self.owner)
        response = client.post(reverse("call_processing_settings"), {"action": "save_rule"})
        self.assertEqual(response.status_code, 403)

    def test_save_does_not_activate_or_create_analysis_jobs(self):
        self.call()
        rule = self.save()
        self.assertEqual(rule.mode, "all_except")
        self.assertIsNone(rule.effective_from)
        self.assertEqual(rule.revision, 1)
        self.assertFalse(CallAnalysis.objects.exists())
        self.assertEqual(CallProcessingRuleAudit.objects.count(), 1)

    def test_own_allowlist_normalizes_numbers(self):
        rule = self.save(employee_id=self.owner.pk, mode="allowlist", numbers_text="+7 999 000 00 01\n8 999 000 00 01")
        self.assertEqual(rule.work_numbers, ["9990000001"])
        self.assertTrue(rule.include_staff)

    def test_other_organization_target_and_non_owner_actor_are_denied(self):
        with self.assertRaises(PermissionDenied):
            self.save(employee_id=self.other_owner.pk)
        with self.assertRaises(PermissionDenied):
            self.save(user=self.employee)
        self.assertFalse(CallProcessingRule.objects.exists())

    def test_another_owner_cannot_read_or_write_private_allowlist(self):
        self.force_other_owner_into_org()
        service.save_rule(user=self.other_owner, organization=self.org, employee_id=self.other_owner.pk,
                          mode="allowlist", include_staff=False, numbers_text="+12025550199", expected_revision=0)
        rows = _rule_rows(self.org, self.owner)
        other = next(row for row in rows if row["id"] == self.other_owner.pk)
        self.assertFalse(other["editable"])
        self.assertIsNone(other["form"])
        with self.assertRaises(PermissionDenied):
            self.save(employee_id=self.other_owner.pk)

    def test_stale_rule_revision_does_not_overwrite(self):
        self.save()
        with self.assertRaises(ValidationError):
            self.save(mode="manual", expected_revision=0)
        self.assertEqual(CallProcessingRule.objects.get().mode, "all_except")
        self.assertEqual(CallProcessingRuleAudit.objects.count(), 1)

    def test_locked_role_is_rechecked_after_early_permission_check(self):
        original = service.settings_allowed

        def revoke_then_return(user, org):
            allowed = original(user, org)
            OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="manager")
            return allowed

        with patch.object(service, "settings_allowed", side_effect=revoke_then_return):
            with self.assertRaises(PermissionDenied):
                self.save()
        self.assertFalse(CallProcessingRule.objects.exists())

    def test_cached_active_actor_does_not_bypass_deactivation(self):
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertTrue(self.owner.is_active)
        with self.assertRaises(PermissionDenied):
            self.save()

    def test_private_numbers_normalize_deduplicate_and_do_not_enter_audit_details(self):
        for _ in range(2):
            service.add_private_numbers(user=self.owner, organization=self.org, label="Private test contact",
                                        numbers_text="+7 999 000 00 01\n8 999 000 00 01")
        self.assertEqual(CallPrivateNumber.objects.count(), 1)
        self.assertEqual(CallProcessingRuleAudit.objects.count(), 1)
        audit = CallProcessingRuleAudit.objects.get()
        self.assertEqual(audit.details, {"changed_count": 1})
        self.assertNotIn("9990000001", str(audit.details))

    def test_cannot_remove_other_owners_private_number(self):
        self.force_other_owner_into_org()
        number = CallPrivateNumber.objects.create(organization=self.org, owner=self.other_owner, label="Other private", phone_key="+12025550199")
        with self.assertRaises(PermissionDenied):
            service.remove_private_number(user=self.owner, organization=self.org, number_id=number.pk)
        self.assertTrue(CallPrivateNumber.objects.filter(pk=number.pk).exists())

    def test_audit_failure_rolls_back_preferences_and_private_numbers(self):
        with patch.object(service, "_audit", side_effect=RuntimeError("test audit failure")):
            with self.assertRaises(RuntimeError):
                self.save()
            with self.assertRaises(RuntimeError):
                service.add_private_numbers(user=self.owner, organization=self.org, label="Private", numbers_text="+12025550199")
        self.assertFalse(CallPrivateNumber.objects.exists())
        self.assertFalse(CallProcessingRule.objects.exists())

    def test_preview_unknown_crm_client_and_audio_wait_without_paid_calls(self):
        self.save()
        ready = self.call()
        pending = self.call(recording_status="pending", recording_file="")
        with patch("pool_service.services.call_ai._client", side_effect=AssertionError("No paid calls")):
            result = self.preview()
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["selected_minutes"], Decimal("4.00"))
        self.assertEqual({row["id"]: row["action"] for row in result["rows"]}, {ready.pk: "transcribe", pending.pk: "wait_audio"})
        self.assertFalse(CallAnalysis.objects.exists())

    def test_preview_is_scoped_to_organization_and_seven_days(self):
        self.save()
        wanted = self.call()
        self.call(started_at=self.now - timedelta(days=8))
        self.call(organization=self.other_org, connection=None, source_kind="uploaded", employee=self.other_owner)
        result = self.preview()
        self.assertEqual([row["id"] for row in result["rows"]], [wanted.pk])

    def test_preview_never_returns_transcript_or_summary(self):
        self.save()
        call = self.call()
        CallAnalysis.objects.create(call=call, status="ready", transcript="DO_NOT_RETURN_PRIVATE_TEXT", summary="DO_NOT_RETURN_SUMMARY")
        result = self.preview()
        self.assertEqual(result["selected_count"], 0)
        self.assertNotIn("DO_NOT_RETURN", str(result))
        self.assertEqual(result["rows"][0]["action"], "exclude")

    def test_private_ready_call_is_omitted_before_analysis_status(self):
        self.save(employee_id=self.owner.pk)
        call = self.call(employee=self.owner)
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner, label="PRIVATE_LABEL", phone_key=call.phone_number)
        CallAnalysis.objects.create(call=call, status="ready", transcript="PRIVATE_TRANSCRIPT")
        result = self.preview()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["own_private_count"], 1)
        self.assertNotIn(call.phone_number, str(result))
        self.assertNotIn("PRIVATE_", str(result))

    def test_owner_private_number_does_not_hide_employees_separate_call(self):
        self.save()
        call = self.call()
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner, label="Private", phone_key=call.phone_number)
        result = self.preview()
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["own_private_count"], 0)

    def test_unconfirmed_or_ambiguous_mapping_is_not_selected(self):
        self.save()
        self.call()
        for identities in ([], self.identity_rows() + [dict(connection_id=self.connection.pk, extension="101", external_user="employee-ref", **{"employee__user_id": self.owner.pk})]):
            with patch.object(service, "_identity_rows", return_value=identities):
                result = service.preview_rules(user=self.owner, organization=self.org, now=self.now)
            self.assertEqual(result["selected_count"], 0)

    def test_internal_call_with_one_unresolved_privacy_counterpart_fails_closed(self):
        self.save()
        service.save_rule(
            user=self.owner, organization=self.org, employee_id=self.owner.pk,
            mode="all_except", include_staff=True, numbers_text="", expected_revision=0,
        )
        call = self.call(
            employee=self.owner,
            peer_employee=self.employee,
            direction=PhoneCall.DIRECTION_INTERNAL,
            provider_extension="201",
            provider_user="+12025550111",
            peer_provider_extension="101",
            peer_provider_user="employee-ref",
        )
        identities = self.identity_rows() + [
            dict(
                connection_id=self.connection.pk,
                extension="201",
                external_user="+12025550111",
                **{"employee__user_id": self.owner.pk},
            )
        ]
        with patch.object(service, "_identity_rows", return_value=identities):
            result = service.preview_rules(
                user=self.owner, organization=self.org, now=self.now
            )
        row = next(item for item in result["rows"] if item["id"] == call.pk)
        self.assertFalse(row["selected"])
        self.assertEqual(row["action"], "exclude")
        self.assertEqual(result["selected_count"], 0)

    def test_preview_cap_reports_partial_sample(self):
        self.save()
        for index in range(4):
            self.call(external_id=f"bounded-{index}")
        with patch.object(service, "PREVIEW_LIMIT", 3):
            result = self.preview()
        self.assertTrue(result["limited"])
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual(result["selected_count"], 3)

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_settings_page_escapes_private_labels_and_never_shows_other_owner_lists(self, _blocked):
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner, label="<script>alert(1)</script>", phone_key="+12025550101")
        CallPrivateNumber.objects.create(organization=self.org, owner=self.other_owner, label="SECRET_OTHER_LABEL", phone_key="+12025550199")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("call_processing_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "SECRET_OTHER_LABEL")
        self.assertNotContains(response, "+12025550199")

    @patch("pool_service.views._redirect_if_access_blocked", return_value=None)
    def test_post_saves_valid_rule_but_unknown_action_cannot_activate(self, _blocked):
        self.client.force_login(self.owner)
        url = reverse("call_processing_settings")
        response = self.client.post(url, {
            "action": "save_rule", "employee_id": self.employee.pk,
            "expected_revision": 0, "mode": "all_except", "include_staff": "on", "numbers_text": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(CallProcessingRule.objects.get().effective_from)
        self.assertEqual(self.client.post(url, {"action": "activate"}).status_code, 400)
        self.assertFalse(CallAnalysis.objects.exists())
