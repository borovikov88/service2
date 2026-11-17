import json
from contextlib import ExitStack
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import ChannelConnection, CommunicationAccess, CommunicationChannel, PhoneCall, TelephonyConnection
from pool_service.crm_commercial_models import CrmAccess, CrmIntakeActivation, CrmLead, CrmLeadSource
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.management.commands import crm_call_diagnostics as diagnostic
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services.call_processing_policy import phone_key
from pool_service.services.crm_inbox import fingerprint, source_scope


class CallDiagnosticsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.org = Organization.objects.create(name='Synthetic organization', crm_v2_read=True,
            crm_v2_write=True, crm_intake_process=True, paid_until=self.now + timedelta(days=2))
        self.actor = get_user_model().objects.create_user('synthetic-operator')
        self.membership = OrganizationAccess.objects.create(organization=self.org, user=self.actor, role='admin')
        self.employee = Employee.objects.create(organization=self.org, user=self.actor, display_name='PRIVATE-NAME')
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.actor, view_leads=True)
        CommunicationAccess.objects.filter(organization=self.org, user=self.actor).update(
            can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=False)
        self.activation = CrmIntakeActivation.objects.create(organization=self.org,
            starts_at=self.now - timedelta(hours=1), last_error='PRIVATE-ERROR')
        channel = CommunicationChannel.objects.create(organization=self.org, kind='megafon', name='PRIVATE-CHANNEL')
        self.channel = ChannelConnection.objects.create(channel=channel, external_id='PRIVATE-ACCOUNT')
        self.telephony = TelephonyConnection.objects.create(organization=self.org, external_id='PRIVATE-ACCOUNT')
        self.call = PhoneCall.objects.create(organization=self.org, connection=self.telephony,
            employee=self.actor, employee_profile=self.employee, external_id='PRIVATE-PROVIDER-ID',
            phone_number='+12025550101', provider_user='PRIVATE-USER', provider_extension='PRIVATE-EXT',
            direction='in', result='answered', duration_seconds=92, started_at=self.now,
            intake_received_at=self.now, intake_occurred_at=self.now,
            intake_historical=False, intake_revision_historical=False, intake_origin='live')
        self.call.intake_scope = source_scope('call', self.call, self.channel, self.telephony)
        self.call.save()

    def event(self, **changes):
        args = dict(organization=self.org, source_type='call', source_id=self.call.pk,
            source_key='a' * 64, provider_kind='megafon', connection_id_snapshot=self.telephony.pk,
            channel_id_snapshot=self.channel.pk, account_id_snapshot=self.telephony.external_id,
            namespace='', external_id=self.call.external_id, scope_verified=True,
            revision=1, fingerprint=fingerprint('call', self.call), occurred_at=self.now,
            received_at=self.now, origin='live', historical=False, next_attempt_at=self.now)
        args.update(changes)
        return CrmInboxEvent.objects.create(**args)

    def link(self, event, lead=None):
        lead = lead or CrmLead.objects.create(organization=self.org, title='PRIVATE-LEAD')
        CrmLeadSource.objects.create(organization=self.org, inbox=event, lead=lead)
        return lead

    def read(self, **changes):
        out, err = StringIO(), StringIO()
        args = dict(organization=self.org.pk, actor=self.actor.pk, call_id=self.call.pk, stdout=out, stderr=err)
        args.update(changes)
        call_command('crm_call_diagnostics', **args)
        self.assertEqual(err.getvalue(), '')
        return json.loads(out.getvalue())

    def unavailable(self, **changes):
        out, err = StringIO(), StringIO()
        with self.assertRaisesMessage(CommandError, diagnostic.UNAVAILABLE) as caught:
            self.read(stdout=out, stderr=err, **changes)
        self.assertEqual(str(caught.exception), diagnostic.UNAVAILABLE)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def test_missing_capture_is_useful_without_repair(self):
        result = self.read()
        self.assertFalse(result['capture_recorded'])
        self.assertTrue(result['source_receipt_present'])
        self.assertEqual(result['source_gate'], 'eligible')
        self.assertEqual(result['revisions'], [])
        self.assertEqual(result['readiness']['activation_present'], True)
        self.assertFalse(CrmInboxEvent.objects.exists())

    def test_pending_failed_and_done_linked(self):
        event = self.event()
        first = self.read()['revisions'][0]
        self.assertEqual((first['state'], first['due'], first['lead_linked']), ('pending', True, False))
        event.state, event.error_code = 'failed', 'consumer_failed'
        event.save()
        self.assertEqual(self.read()['revisions'][0]['error_category'], 'processing_failed')
        event.state, event.error_code, event.processed_at = 'done', 'lead_created', self.now
        event.save()
        self.link(event)
        last = self.read()['revisions'][0]
        self.assertEqual((last['state'], last['lead_linked'], last['error_category']), ('done', True, 'none'))

    def test_revisions_are_history_not_duplicate_leads(self):
        old = self.event(state='done', processed_at=self.now, error_code='lead_created')
        lead = self.link(old)
        self.call.duration_seconds += 1
        self.call.save()
        current = self.event(revision=2, state='done', processed_at=self.now, error_code='lead_linked')
        self.link(current, lead)
        rows = self.read()['revisions']
        self.assertEqual([row['revision'] for row in rows], [2, 1])
        self.assertEqual([row['current_facts_match'] for row in rows], [True, False])
        self.assertTrue(all(row['lead_linked'] for row in rows))
        self.assertNotIn('duplicate', json.dumps(rows))
        self.assertEqual(CrmLead.objects.count(), 1)

    def test_historical_and_activation_cutoff_are_not_current_work(self):
        event = self.event(historical=True)
        self.assertEqual(self.read()['revisions'][0]['activation_gate'], 'historical')
        self.activation.delete()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now + timedelta(seconds=1))
        self.assertEqual(self.read()['source_gate'], 'historical')

    def test_disabled_missing_activation_expired_access_and_unverified(self):
        for field in ('crm_v2_write', 'crm_intake_process'):
            with self.subTest(field=field):
                Organization.objects.filter(pk=self.org.pk).update(**{field: False})
                self.assertEqual(self.read()['source_gate'], 'disabled')
                Organization.objects.filter(pk=self.org.pk).update(**{field: True})
        Organization.objects.filter(pk=self.org.pk).update(paid_until=self.now - timedelta(days=1), trial_started_at=None)
        self.assertEqual(self.read()['source_gate'], 'disabled')
        self.org.save()
        self.activation.delete()
        self.assertEqual(self.read()['source_gate'], 'activation_missing')
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now - timedelta(days=1))
        PhoneCall.objects.filter(pk=self.call.pk).update(intake_scope=None, intake_received_at=None)
        self.assertEqual(self.read()['source_gate'], 'unverified')

    def test_no_content_identifiers_exception_strings_or_private_error_flags(self):
        event = self.event(error_code='PRIVATE-EXCEPTION-PAYLOAD')
        result = self.read()
        encoded = json.dumps(result)
        for marker in ('PRIVATE', self.call.phone_number, 'source_key', 'fingerprint', 'last_error', 'last_run', 'backlog'):
            self.assertNotIn(marker, encoded)
        self.assertEqual(result['revisions'][0]['error_category'], 'other')
        event.error_code = 'private_ignored'
        event.save()
        self.assertNotIn('private', json.dumps(self.read()))
        with patch.object(diagnostic, 'fingerprint', side_effect=RuntimeError('PRIVATE-EXCEPTION')):
            self.unavailable()
        self.assertEqual(diagnostic.STATES, frozenset(dict(CrmInboxEvent._meta.get_field('state').choices)))
        # The DB also constrains state; simulate an unexpected value at the read boundary.
        from_db = CrmInboxEvent.from_db
        def corrupt_state(*args, **kwargs):
            row = from_db(*args, **kwargs)
            row.state = 'PRIVATE-STATE-SECRET'
            return row
        with patch.object(CrmInboxEvent, 'from_db', side_effect=corrupt_state) as load:
            self.unavailable()
            self.assertTrue(load.called)

    def test_absent_foreign_and_invalid_inputs_have_identical_denial(self):
        other = Organization.objects.create(name='Other synthetic')
        for args in ({'call_id': self.call.pk + 999}, {'organization': other.pk}, {'actor': self.actor.pk + 999},
                     {'call_id': 0}, {'call_id': -1}, {'call_id': 2**63}, {'organization': 0}):
            with self.subTest(args=args):
                self.unavailable(**args)

    @override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=True)
    def test_explicit_second_organization_never_uses_default_membership_or_queue(self):
        other = Organization.objects.create(name='Second synthetic', crm_v2_read=True, crm_v2_write=True,
            crm_intake_process=True, paid_until=self.now + timedelta(days=1))
        OrganizationAccess.objects.create(organization=other, user=self.actor, role='admin')
        CrmAccess.objects.create(organization=other, user=self.actor, view_leads=True)
        connection = TelephonyConnection.objects.create(organization=other, external_id='PRIVATE-OTHER')
        call = PhoneCall.objects.create(organization=other, connection=connection, external_id='PRIVATE-OTHER-CALL',
            phone_number='+12025550102', direction='in', result='answered', started_at=self.now)
        self.event(state='failed', error_code='consumer_failed')
        self.unavailable(organization=other.pk)
        result = self.read(organization=other.pk, call_id=call.pk)
        self.assertEqual(result['organization_id'], other.pk)
        self.assertFalse(result['capture_recorded'])
        self.assertEqual(result['revisions'], [])
        self.assertEqual(result['readiness']['event_wakeup_gate'], 'outside_pilot')

    def test_current_policy_revocations_and_no_staff_or_superuser_shortcut(self):
        changes = [(self.actor, 'is_active', False), (self.grant, 'view_leads', False),
                   (self.employee, 'is_active', False), (self.employee, 'employment_status', 'dismissed'),
                   (self.org, 'crm_v2_read', False), (self.membership, 'role', 'manager')]
        for row, field, value in changes:
            with self.subTest(field=field):
                before = getattr(row, field)
                setattr(row, field, value); row.save()
                self.unavailable()
                setattr(row, field, before); row.save()
        self.actor.is_superuser = True; self.actor.save()
        self.grant.view_leads = False; self.grant.save()
        self.unavailable()
        self.grant.view_leads = True; self.grant.save()
        self.membership.delete()
        self.unavailable()

    def test_communication_revocation_own_scope_and_no_listening_grant_needed(self):
        access = CommunicationAccess.objects.get(organization=self.org, user=self.actor)
        access.can_view_all_calls = False; access.can_view_own_calls = True; access.save()
        self.assertTrue(self.read()['answered'])
        PhoneCall.objects.filter(pk=self.call.pk).update(employee=None, employee_profile=None)
        self.unavailable()
        PhoneCall.objects.filter(pk=self.call.pk).update(peer_employee=self.actor)
        self.assertTrue(self.read()['answered'])
        access.can_view_own_calls = False; access.save()
        self.unavailable()

    def test_owner_peer_privacy_is_pair_specific_and_blocks_owner_and_superuser(self):
        owner = get_user_model().objects.create_user('synthetic-owner')
        OrganizationAccess.objects.create(organization=self.org, user=owner, role='owner')
        CrmAccess.objects.create(organization=self.org, user=owner, view_leads=True)
        CallPrivateNumber.objects.create(organization=self.org, owner=owner, phone_key=phone_key(self.call.phone_number))
        # Same peer with another employee remains a work call.
        self.assertTrue(self.read()['answered'])
        PhoneCall.objects.filter(pk=self.call.pk).update(peer_employee=owner)
        self.unavailable()
        self.actor.is_superuser = True; self.actor.save()
        self.unavailable()
        self.unavailable(actor=owner.pk)
        PhoneCall.objects.filter(pk=self.call.pk).update(peer_employee=None, employee=owner, employee_profile=None)
        self.unavailable()

    def test_binding_revocation_and_foreign_graph_are_unavailable(self):
        other = Organization.objects.create(name='Other synthetic')
        foreign = Client.objects.create(organization=other, name='PRIVATE-FOREIGN')
        PhoneCall.objects.filter(pk=self.call.pk).update(client=foreign)
        self.unavailable()
        PhoneCall.objects.filter(pk=self.call.pk).update(client=None)
        for row, field, bad in ((self.telephony, 'is_active', False), (self.telephony, 'external_id', 'CHANGED'),
                                (self.channel, 'is_active', False)):
            old = getattr(row, field); setattr(row, field, bad); row.save()
            self.unavailable()
            setattr(row, field, old); row.save()
        scope = {**self.call.intake_scope, 'organization': other.pk}
        PhoneCall.objects.filter(pk=self.call.pk).update(intake_scope=scope)
        self.unavailable()

    def test_unreadable_related_lead_does_not_reveal_link_existence(self):
        event = self.event(state='done', processed_at=self.now)
        lead = self.link(event)
        foreign = Organization.objects.create(name='Foreign synthetic')
        client = Client.objects.create(organization=foreign, name='PRIVATE-FOREIGN')
        # Deliberately corrupt only this synthetic graph, bypassing write validation.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE pool_service_crmlead SET client_id=%s WHERE id=%s', [client.pk, lead.pk])
        self.unavailable()

    def test_private_source_preserves_independent_work_diagnostic_metadata(self):
        event = self.event(state='done', processed_at=self.now)
        lead = self.link(event)
        owner = get_user_model().objects.create_user('synthetic-private-owner')
        OrganizationAccess.objects.create(organization=self.org, user=owner, role='owner')
        private = PhoneCall.objects.create(organization=self.org, connection=self.telephony,
            employee=owner, external_id='PRIVATE-SECOND-CALL', phone_number='+12025550102',
            direction='in', result='answered', started_at=self.now,
            intake_received_at=self.now, intake_occurred_at=self.now)
        private.intake_scope = source_scope('call', private, self.channel, self.telephony)
        private.save()
        other = self.event(source_id=private.pk, source_key='b' * 64, external_id=private.external_id,
            state='done', processed_at=self.now)
        self.link(other, lead)
        CallPrivateNumber.objects.create(organization=self.org, owner=owner, phone_key=phone_key(private.phone_number))
        result = self.read()
        self.assertEqual(len(result['revisions']), 1)
        self.assertTrue(result['revisions'][0]['lead_linked'])
        self.assertEqual(result['call_id'], self.call.pk)
        self.assertNotIn(private.external_id, json.dumps(result))

    def test_superuser_revocation_is_read_from_database(self):
        self.actor.is_superuser = True; self.actor.save()
        CommunicationAccess.objects.filter(organization=self.org, user=self.actor).update(
            can_view_all_calls=False, can_view_own_calls=False)
        self.assertTrue(self.read()['answered'])
        get_user_model().objects.filter(pk=self.actor.pk).update(is_superuser=False)
        self.unavailable()

    def test_rechecks_revocation_after_reading_chain(self):
        self.event()
        authorize = diagnostic._authorized
        checks = []
        def revoke(*args):
            if checks:
                CommunicationAccess.objects.filter(organization=self.org, user=self.actor).update(
                    can_view_all_calls=False, can_view_own_calls=False)
            checks.append(True)
            return authorize(*args)
        with patch.object(diagnostic, '_authorized', side_effect=revoke):
            self.unavailable()
        self.assertEqual(len(checks), 2)

    def test_only_selects_no_files_provider_ai_worker_or_wakeup(self):
        event = self.event()
        models = (CrmIntakeActivation, CrmInboxEvent, PhoneCall, CrmLead, CrmLeadSource)
        targets = ('pool_service.services.crm_inbox.capture_source', 'pool_service.services.crm_inbox.reconcile',
            'pool_service.services.crm_inbox.run_worker', 'pool_service.services.crm_lead_intake.consume_incoming',
            'pool_service.services.crm_intake_wakeup.schedule_wakeup', 'pool_service.services.call_ai.request_call_analysis',
            'pool_service.services.call_ai.start_requested_call_analysis_worker', 'requests.sessions.Session.request',
            'subprocess.Popen', 'socket.socket', 'fcntl.flock', 'os.open', 'builtins.open')
        attempted_writes = []
        def select_only(execute, sql, params, many, context):
            if not sql.lstrip().upper().startswith('SELECT'):
                attempted_writes.append(sql)
                raise AssertionError('Unexpected non-SELECT SQL')
            return execute(sql, params, many, context)
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    event.state, event.processed_at = 'done', self.now
                    event.save()
                    self.link(event)
                before = [list(model.objects.values()) for model in models]
                with ExitStack() as stack:
                    mocks = [stack.enter_context(patch(target, side_effect=AssertionError('Unexpected side effect'))) for target in targets]
                    stack.enter_context(connection.execute_wrapper(select_only))
                    result = self.read()
                    self.assertTrue(result['capture_recorded'])
                    self.assertEqual(result['revisions'][0]['lead_linked'], linked)
                    self.unavailable(call_id=self.call.pk + 999)
                    for mock in mocks:
                        mock.assert_not_called()
                # This assertion survives the command's catch-all on denied reads.
                self.assertEqual(attempted_writes, [])
                self.assertEqual(before, [list(model.objects.values()) for model in models])

    @override_settings(CRM_INTAKE_EVENT_WAKEUP_ENABLED=True)
    def test_history_is_bounded_and_wakeup_is_configuration_only(self):
        for revision in range(1, 23):
            self.event(revision=revision)
        result = self.read()
        self.assertTrue(result['more_revisions'])
        self.assertEqual([row['revision'] for row in result['revisions']], list(range(22, 2, -1)))
        self.assertTrue(result['readiness']['event_wakeup_enabled'])
