from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.crm_commercial_models import CrmAccess, CrmLead
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.services.call_access import can_read_call
from pool_service.services.department_structure import assign_employee, create_department, end_assignment
from pool_service.staff_call_models import CallWorkContext, StaffAccount
from pool_service.staff_structure_models import DepartmentAssignment


class DepartmentStructureTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Test org', crm_v2_read=True)
        self.owner = self.staff('owner', 'owner')
        self.head = self.staff('head', 'manager')
        self.manager = self.staff('manager', 'manager')
        self.dept = create_department(self.owner, self.org, name='Sales', parent_id=None)

    def staff(self, username, role):
        user = get_user_model().objects.create_user('synthetic-dept-' + username)
        OrganizationAccess.objects.create(organization=self.org, user=user, role=role)
        StaffAccount.objects.create(organization=self.org, user=user, kind='person')
        Employee.objects.create(organization=self.org, user=user, display_name=username)
        CrmAccess.objects.create(organization=self.org, user=user, view_leads=True)
        CommunicationAccess.objects.filter(organization=self.org, user=user).update(
            can_view_all_calls=True, can_listen_calls=True)
        return user

    def assign(self):
        return assign_employee(self.owner, self.org, department_id=self.dept.pk,
            employee_id=Employee.objects.get(user=self.head).pk, position='sales_head', includes_children=True)

    def test_director_can_create_child_and_fill_vacancy_once(self):
        url = reverse('staff_departments', args=[self.org.pk])
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), 'Вакантно')
        self.assertEqual(self.client.post(url, {'action':'create','name':'Retail','parent':self.dept.pk}).status_code, 302)
        row = self.assign()
        self.assertTrue(row.includes_children)
        self.assertEqual(row.user_at_assignment, self.head)
        with self.assertRaises(ValidationError):
            self.assign()
        with self.assertRaises(PermissionDenied):
            end_assignment(self.manager, self.org, assignment_id=row.pk, keep_cards=True)
        with self.assertRaises(ValidationError):
            end_assignment(self.owner, self.org, assignment_id=row.pk, keep_cards=False)
        self.assertEqual(DepartmentAssignment.objects.count(), 1)

    def test_foreign_parent_and_unverified_employee_are_rejected(self):
        other = Organization.objects.create(name='Test foreign org')
        from pool_service.staff_structure_models import StaffDepartment
        foreign = StaffDepartment.objects.create(organization=other, name='Foreign sales', created_by=self.owner)
        with self.assertRaises(ValidationError):
            create_department(self.owner, self.org, name='Untrusted parent', parent_id=foreign.pk)
        StaffAccount.objects.filter(user=self.head, organization=self.org).update(kind='service')
        with self.assertRaises(ValidationError):
            self.assign()

    def test_leader_recording_is_protected_after_end_and_respects_current_primary_assignee(self):
        row = self.assign()
        connection = TelephonyConnection.objects.create(organization=self.org, external_id='test-dept-line')
        call = PhoneCall.objects.create(organization=self.org, connection=connection, external_id='test-dept-call',
            direction='out', employee=self.head, phone_number='+79001234567', started_at=timezone.now())
        self.assertEqual(call.sales_head_user_snapshot, self.head.pk)
        self.assertTrue(can_read_call(self.head, call, media=True))
        self.assertFalse(can_read_call(self.manager, call, media=True))
        lead = CrmLead.objects.create(organization=self.org, assignee=self.manager, title='Commercial card')
        CallWorkContext.objects.create(organization=self.org, call=call, lead=lead, changed_by=self.owner)
        self.assertTrue(can_read_call(self.manager, call, media=True))
        lead.assignee = self.owner
        lead.save()
        end_assignment(self.owner, self.org, assignment_id=row.pk, keep_cards=True)
        call.save()
        self.assertEqual(call.sales_head_user_snapshot, self.head.pk)
        self.assertFalse(can_read_call(self.manager, call, media=True))

    def test_late_phone_identity_mapping_keeps_the_leader_restriction(self):
        from pool_service.services.employee_identity_sync import backfill_employee_calls
        self.assign()
        employee = Employee.objects.get(user=self.head)
        connection = TelephonyConnection.objects.create(organization=self.org, external_id='test-late-map')
        call = PhoneCall.objects.create(organization=self.org, connection=connection, employee_profile=employee,
            external_id='late', direction='out', phone_number='+79001234567', started_at=timezone.now())
        self.assertFalse(can_read_call(self.manager, call, media=True))
        backfill_employee_calls(employee)
        call.refresh_from_db()
        self.assertEqual(call.employee_id, self.head.pk)
        self.assertEqual(call.sales_head_user_snapshot, self.head.pk)
        self.assertFalse(can_read_call(self.manager, call, media=True))

    def test_dismissal_frees_the_position_and_remapping_requires_a_finished_appointment(self):
        from pool_service.services.employee_identity_sync import EmployeeIdentitySyncError, map_employee_service2_user
        from pool_service.services.staff_lifecycle import dismiss_employee
        row = self.assign()
        employee = Employee.objects.get(user=self.head)
        replacement = get_user_model().objects.create_user('synthetic-new-dept-account')
        OrganizationAccess.objects.create(organization=self.org, user=replacement, role='manager')
        with self.assertRaises(EmployeeIdentitySyncError):
            map_employee_service2_user(employee, replacement, self.owner)
        dismiss_employee(actor=self.owner, employee_id=employee.pk, expected_updated_at=employee.updated_at.isoformat())
        row.refresh_from_db()
        self.assertEqual(row.ended_by, self.owner)
        self.assertIsNotNone(row.valid_to)
        assign_employee(self.owner, self.org, department_id=self.dept.pk,
            employee_id=Employee.objects.get(user=self.manager).pk, position='sales_head', includes_children=False)

    def test_undated_delivery_stays_restricted_until_provider_date_is_known(self):
        row = self.assign()
        end_assignment(self.owner, self.org, assignment_id=row.pk, keep_cards=True)
        connection = TelephonyConnection.objects.create(organization=self.org, external_id='test-undated')
        now = timezone.now()
        call = PhoneCall.objects.create(organization=self.org, connection=connection, employee=self.head,
            external_id='late', direction='out', phone_number='+79001234567', started_at=now,
            intake_received_at=now, intake_scope={'provider': 'megafon'})
        self.assertIsNone(call.sales_head_user_snapshot)
        self.assertFalse(can_read_call(self.manager, call, media=True))
        call.intake_occurred_at = now
        call.save()
        self.assertTrue(can_read_call(self.manager, call, media=True))

    def test_bulk_backfill_of_undated_calls_preserves_review_until_the_actual_time(self):
        from pool_service.services.employee_identity_sync import backfill_employee_calls
        row = self.assign()
        in_office = timezone.now()
        end_assignment(self.owner, self.org, assignment_id=row.pk, keep_cards=True)
        received = timezone.now()
        employee = Employee.objects.get(user=self.head)
        connection = TelephonyConnection.objects.create(organization=self.org, external_id='pending-and-reassigned')
        for key, actual, allow in [('during', in_office, False), ('after', received, True)]:
            call = PhoneCall.objects.create(organization=self.org, connection=connection, employee_profile=employee,
                external_id=key, direction='out', phone_number='+79001234567', started_at=received,
                intake_received_at=received, intake_scope={'provider': 'megafon'})
            backfill_employee_calls(employee)
            call.refresh_from_db()
            self.assertIsNone(call.sales_head_user_snapshot)
            self.assertEqual(call.undated_sales_head, self.head.pk)
            self.assertFalse(can_read_call(self.manager, call, media=True))
            call.intake_occurred_at = call.started_at = actual
            call.save()
            self.assertEqual(can_read_call(self.manager, call, media=True), allow)
