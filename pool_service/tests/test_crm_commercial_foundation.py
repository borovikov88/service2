from decimal import Decimal
from io import StringIO

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.forms import modelform_factory
from django.test import RequestFactory
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import (
    CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmLead, CrmPipeline, CrmStage,
)
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services.crm_commercial_access import (
    can_view_team_deals, can_write_commercial, deals_for_user,
)


FLAGS = ("crm_v2_read", "crm_v2_write", "crm_intake_process", "crm_materials",
         "crm_accounting", "crm_close", "tender_mail", "tender_autoselect")


class CommercialFoundationTests(TestCase):
    def setUp(self):
        self.a = Organization.objects.create(name="CRM A")
        self.b = Organization.objects.create(name="CRM B")
        self.manager = get_user_model().objects.create_user("crm-manager")
        self.other = get_user_model().objects.create_user("crm-other")
        OrganizationAccess.objects.create(organization=self.a, user=self.manager, role="manager")
        OrganizationAccess.objects.create(organization=self.a, user=self.other, role="manager")
        self.pa = CrmPipeline.objects.create(organization=self.a, code="a", name="A")
        self.pb = CrmPipeline.objects.create(organization=self.b, code="b", name="B")
        self.sa = CrmStage.objects.create(pipeline=self.pa, code="new", name="New")
        self.sb = CrmStage.objects.create(pipeline=self.pb, code="new", name="New")
        self.da = CrmDeal.objects.create(organization=self.a, pipeline=self.pa, stage=self.sa, manager=self.other, title="Team A")
        self.db = CrmDeal.objects.create(organization=self.b, pipeline=self.pb, stage=self.sb, title="Team B")

    def test_flags_off_and_no_second_writer(self):
        self.assertTrue(all(getattr(self.a, flag) is False for flag in FLAGS))
        self.assertFalse(can_view_team_deals(self.manager, self.a))
        self.assertFalse(deals_for_user(self.manager, self.a).exists())
        Organization.objects.filter(pk=self.a.pk).update(crm_v2_read=True)
        self.assertTrue(can_view_team_deals(self.manager, self.a))
        self.assertFalse(can_write_commercial(self.manager, self.a))
        self.a.refresh_from_db()
        self.assertFalse(self.a.crm_v2_write)
        self.assertTrue(all(not getattr(self.a, flag) for flag in FLAGS if flag != "crm_v2_read"))
        self.assertFalse(CrmAccess.objects.exists())

    def test_current_account_membership_employee_and_team_scope(self):
        Organization.objects.filter(pk=self.a.pk).update(crm_v2_read=True)
        self.assertEqual(list(deals_for_user(self.manager, self.a)), [self.da])
        self.assertFalse(can_view_team_deals(self.manager, self.b))
        # Persisted changes are honored even with stale user and org instances.
        get_user_model().objects.filter(pk=self.manager.pk).update(is_active=False)
        self.assertFalse(can_view_team_deals(self.manager, self.a))
        get_user_model().objects.filter(pk=self.manager.pk).update(is_active=True)
        employee = Employee.objects.create(organization=self.a, user=self.manager, display_name="Synthetic")
        for changes in ({"is_active": False}, {"is_active": True, "employment_status": Employee.STATUS_DISMISSED}):
            Employee.objects.filter(pk=employee.pk).update(**changes)
            self.assertFalse(can_view_team_deals(self.manager, self.a))
        Employee.objects.filter(pk=employee.pk).update(is_active=True, employment_status=Employee.STATUS_EMPLOYED)
        self.assertTrue(can_view_team_deals(self.manager, self.a))
        OrganizationAccess.objects.filter(organization=self.a, user=self.manager).delete()
        self.assertFalse(can_view_team_deals(self.manager, self.a))
        CrmAccess.objects.create(organization=self.a, user=self.manager, view_team_deals=True)
        self.assertFalse(can_view_team_deals(self.manager, self.a))

    def test_other_roles_require_explicit_scoped_read_and_revoke(self):
        Organization.objects.filter(pk=self.a.pk).update(crm_v2_read=True)
        membership = OrganizationAccess.objects.get(organization=self.a, user=self.manager)
        membership.role = "accountant"
        membership.save()
        self.assertFalse(can_view_team_deals(self.manager, self.a))
        grant = CrmAccess.objects.create(organization=self.a, user=self.manager, view_team_deals=True)
        self.assertTrue(can_view_team_deals(self.manager, self.a))
        self.assertFalse(can_write_commercial(self.manager, self.a))
        grant.view_team_deals = False
        grant.save(update_fields=["view_team_deals"])
        self.assertFalse(can_view_team_deals(self.manager, self.a))
        self.assertFalse(can_view_team_deals(get_user_model().objects.create_superuser("root", password="test"), self.a))

    def test_cross_org_stage_client_and_assignment_rejected_on_save(self):
        foreign_client = Client.objects.create(organization=self.b, name="Synthetic B")
        unscoped_client = Client.objects.create(name="Synthetic no org")
        for changes in ({"stage": self.sb}, {"pipeline": self.pb, "stage": self.sb},
                        {"client": foreign_client}, {"client": unscoped_client}):
            deal = CrmDeal.objects.get(pk=self.da.pk)
            for key, value in changes.items():
                setattr(deal, key, value)
            with self.assertRaises(ValidationError):
                deal.save()
        OrganizationAccess.objects.filter(user=self.other).delete()
        self.da.title = "Changed with historical assignment"
        self.da.save(update_fields=["title"])
        self.assertEqual(self.da.manager_id, self.other.pk)
        with self.assertRaises(ValidationError):
            CrmDeal.objects.create(organization=self.a, pipeline=self.pa, stage=self.sa, manager=self.other, title="New unavailable assignment")

    def test_unsafe_batch_and_expression_writes_fail_closed(self):
        for model in (CrmPipeline, CrmStage, CrmLead, CrmDeal, CrmCommand, CrmEvent, CrmAccess):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    model.objects.all().update(organization_id=self.b.pk)
                with self.assertRaises(ValidationError):
                    model.objects.bulk_create([])
                with self.assertRaises(ValidationError):
                    model.objects.bulk_update([], ["id"])
        with self.assertRaises(ValidationError):
            CrmDeal._base_manager.filter(pk=self.da.pk).update(stage=self.sb)
        with self.assertRaises(ValidationError):
            CrmDeal.objects.filter(pk=self.da.pk).update(stage=self.sb)
        with self.assertRaises(ValidationError):
            CrmDeal.objects.bulk_create([CrmDeal(organization=self.a, pipeline=self.pa, stage=self.sb, title="Invalid")])

    def test_immutable_scope_partial_save_and_parent_delete(self):
        self.pa.organization = self.b
        with self.assertRaises(ValidationError):
            self.pa.save()
        self.sa.pipeline = self.pb
        with self.assertRaises(ValidationError):
            self.sa.save()
        self.da.stage = self.sb
        self.da.title = "Changed"
        with self.assertRaises(ValidationError):
            self.da.save(update_fields=["title"])
        with self.assertRaises(ProtectedError):
            CrmPipeline.objects.filter(pk=self.pa.pk).delete()
        for delete in (lambda: self.sa.delete(), lambda: CrmStage.objects.filter(pk=self.sa.pk).delete()):
            with self.assertRaises(ValidationError):
                delete()
        self.sa.refresh_from_db()
        self.sa.is_active = False
        self.sa.save(update_fields=["is_active"])
        self.da.refresh_from_db()
        self.assertEqual(self.da.stage_id, self.sa.pk)

    def test_legacy_client_reparent_hides_new_card_without_blocking_legacy(self):
        client = Client.objects.create(organization=self.a, name="Synthetic")
        self.da.client = client
        self.da.save()
        Organization.objects.filter(pk=self.a.pk).update(crm_v2_read=True)
        self.assertTrue(deals_for_user(self.manager, self.a).filter(pk=self.da.pk).exists())
        Client.objects.filter(pk=client.pk).update(organization=self.b)
        self.assertFalse(deals_for_user(self.manager, self.a).filter(pk=self.da.pk).exists())
        self.da.title = "No stale link"
        with self.assertRaises(ValidationError):
            self.da.save()
        # Repair is explicit; no history or legacy row is rewritten automatically.
        self.da.client = None
        self.da.save()

    def test_tender_without_phone_budget_client_and_budget_shapes(self):
        lead = CrmLead.objects.create(organization=self.a, kind="tender", title="Synthetic tender")
        self.assertIsNone(lead.client_id)
        self.assertIsNone(lead.budget_min)
        self.assertIsNone(lead.budget_max)
        for changes in ({"budget_kind": "exact", "budget_min": 10, "budget_max": 11, "currency": "RUB"},
                        {"budget_kind": "range", "budget_min": 20, "budget_max": 10, "currency": "RUB"},
                        {"budget_kind": "exact", "budget_min": 10, "budget_max": 10, "currency": ""},
                        {"budget_min": 0}, {"status": "waiting"}):
            obj = CrmLead.objects.get(pk=lead.pk)
            for key, value in changes.items():
                setattr(obj, key, value)
            with self.assertRaises(ValidationError):
                obj.save()
        lead.budget_kind = "exact"
        lead.budget_min = lead.budget_max = Decimal("0")
        lead.currency = "RUB"
        lead.save()
        self.assertEqual(lead.budget_min, 0)
        lead.status = "closed"
        lead.result = "no_deal"
        with self.assertRaises(ValidationError):
            lead.save()
        lead.result = "spam"
        lead.save()
        self.assertEqual(lead.result, "spam")

    def test_flags_hidden_from_automatic_forms_and_no_new_admin_writers(self):
        form = modelform_factory(Organization, fields="__all__")
        self.assertFalse(set(FLAGS) & set(form.base_fields))
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser("admin-check", password="test")
        admin_form = admin.site._registry[Organization].get_form(request)
        self.assertFalse(set(FLAGS) & set(admin_form.base_fields))
        self.assertTrue(all(model not in admin.site._registry for model in (CrmLead, CrmDeal, CrmPipeline, CrmStage, CrmEvent, CrmCommand, CrmAccess)))

    def test_canonical_keys_are_ascii_lowercase_on_every_supported_write(self):
        for code in ("UPPER", "with space", "café", "trailing ", "final\n"):
            with self.assertRaises(ValidationError):
                CrmPipeline.objects.create(organization=self.a, code=code, name="Rejected")
            with self.assertRaises(ValidationError):
                CrmStage.objects.create(pipeline=self.pa, code=code, name="Rejected")
            with self.assertRaises(ValidationError):
                CrmCommand.objects.create(organization=self.a, operation_key=code, command_type="future", payload_hash="a" * 64)
        pipeline = CrmPipeline.objects.create(organization=self.a, code="ascii-123_key", name="Canonical")
        with self.assertRaises(ValidationError):
            CrmPipeline.objects.create(organization=self.a, code=pipeline.code, name="Duplicate")

    def test_command_uniqueness_hash_and_no_private_payloads(self):
        command = CrmCommand.objects.create(organization=self.a, operation_key="op", command_type="future", payload_hash="a" * 64)
        with self.assertRaises(ValidationError):
            CrmCommand.objects.create(organization=self.a, operation_key="other", command_type="future", payload_hash="not a hash")
        with self.assertRaises(ValidationError):
            CrmCommand.objects.create(organization=self.a, operation_key="other", command_type="future", payload_hash="b" * 64, result={"transcript": "private"})
        with self.assertRaises(ValidationError):
            CrmCommand.objects.create(organization=self.a, operation_key="op", command_type="future", payload_hash="a" * 64)
        command.payload_hash = "b" * 64
        with self.assertRaises(ValidationError):
            command.save()

    def test_event_subject_scope_safe_diff_and_immutable_history(self):
        lead = CrmLead.objects.create(organization=self.a, title="Lead")
        command_b = CrmCommand.objects.create(organization=self.b, operation_key="op", command_type="future", payload_hash="b" * 64)
        for values in ({}, {"lead": lead, "deal": self.da}, {"deal": self.db}, {"deal": self.da, "command": command_b},
                       {"deal": self.da, "safe_diff": {"message": "private"}},
                       {"deal": self.da, "safe_diff": {"stage_id": {"before": None, "after": "private"}}}):
            with self.assertRaises(ValidationError):
                CrmEvent.objects.create(organization=self.a, event_type="schema_test", **values)
        event = CrmEvent.objects.create(organization=self.a, deal=self.da, event_type="schema_test", safe_diff={"revision": {"before": 0, "after": 1}})
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            CrmEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ProtectedError):
            self.da.delete()

    def test_bootstrap_repeat_preserves_user_settings_flags_and_legacy(self):
        from pool_service.models import CrmItem
        legacy = CrmItem.objects.create(organization=self.a, title="Legacy", direction="project", stage="prj_idea")
        before = CrmItem.objects.filter(pk=legacy.pk).values().get()
        self.assertEqual(bootstrap_commercial_settings(self.a.pk), (4, 30))
        pipeline = CrmPipeline.objects.get(organization=self.a, code="construction")
        stage = pipeline.stages.get(code="need")
        pipeline.name, pipeline.position, pipeline.is_active = "Custom", 99, False
        pipeline.save()
        stage.name, stage.position, stage.is_active = "Custom", 99, False
        stage.save()
        self.assertEqual(bootstrap_commercial_settings(self.a.pk), (0, 0))
        out = StringIO()
        call_command("bootstrap_crm_commercial", organization_id=self.a.pk, stdout=out)
        self.assertIn("pipelines=0, stages=0", out.getvalue())
        pipeline.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual((pipeline.name, pipeline.position, pipeline.is_active), ("Custom", 99, False))
        self.assertEqual((stage.name, stage.position, stage.is_active), ("Custom", 99, False))
        self.a.refresh_from_db()
        self.assertTrue(all(not getattr(self.a, flag) for flag in FLAGS))
        self.assertFalse(CrmAccess.objects.exists())
        self.assertEqual(before, CrmItem.objects.filter(pk=legacy.pk).values().get())
