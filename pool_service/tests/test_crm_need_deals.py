"""Synthetic selected-line conversion, current authority and retained provenance."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from django.urls import reverse

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmPipeline, CrmDealNextAction
from pool_service.crm_object_need_models import CrmObjectNeedLine, CrmObjectNeedReviewLine, CrmObjectNeedDealLine
from pool_service.models import Client, CrmItem, Employee, Organization, OrganizationAccess, Pool, ServiceTask, WaterReading
from pool_service.services.crm_commercial_commands import create_commercial_deal, CommercialConflict, _pilot_payload, _next_action_values
from pool_service.services.crm_need_deals import convert_need_positions_to_deal, reconcile_need_positions, retained_lines
from pool_service.services.crm_object_needs import NeedConflict, materials_hash
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_object_needs import ReviewFixture


class NeedDealFixture(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.grant.create_own_deals = True
        self.grant.manage_own_next_action = True
        self.grant.save()
        bootstrap_commercial_settings(self.org.pk)
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code='retail')
        positions = self.payload()['positions']
        positions.append(dict(line_token='repair_1', description='Independent repair', mode='approval_required', reason='Separate scope'))
        self.execute(positions=positions)

    def source(self):
        self.need.refresh_from_db()
        self.task.refresh_from_db()
        self.reading.refresh_from_db()
        review = self.need.reviews.get(sequence=self.need.review_revision)
        return dict(task_id=self.task.pk, source_revision=self.need.source_revision,
            source_hash=materials_hash(self.reading.required_materials.strip()), review_id=review.pk,
            review_revision=review.sequence, task_version=version_for(self.task))

    def reconcile_payload(self, tokens=('pump_1', 'repair_1'), key='reconcile-1'):
        return dict(self.source(), operation_key=key,
            selections=[dict(line_token=token, kind='new', line_id=None) for token in tokens])

    def reconcile(self, payload=None):
        return reconcile_need_positions(actor=self.user, organization=self.org, **(payload or self.reconcile_payload()))

    def convert_payload(self, tokens=('pump_1', 'repair_1'), key='convert-1'):
        review_id = self.source()['review_id']
        bindings = {binding.line_token: binding.line_id for binding in CrmObjectNeedReviewLine.objects.filter(review_id=review_id)}
        return dict(self.source(), operation_key=key, client_id=self.client_record.pk, pipeline_id=self.pipeline.pk,
            title='Explicit commercial title', next_action=dict(title='Ask client about scope', due_at='2026-11-01T12:30:00+07:00'),
            selections=[dict(line_token=token, line_id=bindings[token]) for token in tokens])

    def convert(self, payload=None):
        return convert_need_positions_to_deal(actor=self.user, organization=self.org, **(payload or self.convert_payload()))


class NeedDealTests(NeedDealFixture, TestCase):
    def test_selected_group_creates_real_task_without_touching_operational_source(self):
        before = {model: model.objects.values().get(pk=pk) for model, pk in (
            (ServiceTask, self.task.pk), (CrmItem, self.task.crm_item_id), (WaterReading, self.reading.pk))}
        reviews = list(self.need.reviews.values())
        self.reconcile()
        payload = self.convert_payload()
        result = self.convert(payload)
        self.assertEqual(self.convert(payload), result)
        self.assertEqual(CrmDeal.objects.count(), 1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(), 2)
        self.assertEqual(CrmCommand.objects.filter(command_type='deal.create').count(), 1)
        self.assertEqual(CrmEvent.objects.filter(event_type='deal.create').count(), 1)
        deal = CrmDeal.objects.get()
        self.assertEqual((deal.manager_id, deal.client_id, deal.pool_id, deal.object_kind),
            (self.user.pk, self.client_record.pk, self.pool.pk, 'pool'))
        self.assertEqual(deal.work_subject, '')
        followup = CrmDealNextAction.objects.get(deal=deal).task_link.task
        self.assertEqual((followup.pk, followup.task_type, followup.visibility), (result['task_id'], 'crm_followup', 'private'))
        self.assertEqual(followup.primary_responsible_id, self.user.pk)
        self.assertEqual(followup.due_at.isoformat(), '2026-11-01T05:30:00+00:00')
        for model, row in before.items():
            self.assertEqual(model.objects.values().get(pk=row['id']), row)
        self.assertEqual(list(self.need.reviews.values()), reviews)
        self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)

    def test_disjoint_groups_and_new_keys_overlaps_never_release_consumption(self):
        self.reconcile()
        first = self.convert(self.convert_payload(('pump_1',), 'first'))
        second = self.convert(self.convert_payload(('repair_1',), 'second'))
        self.assertNotEqual(first['deal_id'], second['deal_id'])
        for tokens in (('pump_1',), ('pump_1','repair_1')):
            with self.assertRaises(NeedConflict): self.convert(self.convert_payload(tokens, 'new-key'))
        deal = CrmDeal.objects.get(pk=first['deal_id'])
        deal.outcome, deal.is_archived = 'cancelled', True
        deal.save()
        with self.assertRaises(NeedConflict): self.convert(self.convert_payload(('pump_1',), 'after-close'))
        self.assertEqual(CrmDeal.objects.count(), 2)

    def test_new_source_requires_explicit_retained_identity_and_claim_survives_reviews(self):
        self.reconcile()
        first_payload = self.convert_payload(('pump_1',))
        result = self.convert(first_payload)
        old_line = CrmObjectNeedDealLine.objects.get(deal_id=result['deal_id']).line_id
        self.reading.required_materials = 'Changed pump and new independent item'
        self.reading.save()
        with self.assertRaises(NeedConflict): self.convert(first_payload)
        self.execute(operation_key='review-new-source', positions=[
            dict(line_token='new_pump_token', description='Changed pump text', mode='approval_required', reason='Same earlier need'),
            dict(line_token='new_part', description='Independent new part', mode='approval_required', reason='Independent new need')])
        self.reconcile(dict(self.source(), operation_key='map-new-source', selections=[
            dict(line_token='new_pump_token', kind='existing', line_id=old_line),
            dict(line_token='new_part', kind='new', line_id=None)]))
        with self.assertRaises(NeedConflict): self.convert(self.convert_payload(('new_pump_token',), 'again'))
        self.convert(self.convert_payload(('new_part',), 'new-independent'))
        self.assertEqual(CrmDeal.objects.count(), 2)
        self.assertEqual(self.need.sources.count(), 2)
        self.assertEqual(self.need.reviews.count(), 2)

    def test_known_token_cannot_remint_identity_and_bindings_are_immutable(self):
        payload = self.reconcile_payload(('pump_1',))
        result = self.reconcile(payload)
        self.assertEqual(self.reconcile(payload), result)
        with self.assertRaises(NeedConflict): self.reconcile(dict(payload, operation_key='mint-again'))
        self.execute(operation_key='review-another', positions=self.payload()['positions'])
        with self.assertRaises(NeedConflict): self.reconcile(self.reconcile_payload(('pump_1',), 'remint-review'))
        line = CrmObjectNeedLine.objects.get()
        self.reconcile(dict(self.source(), operation_key='same-known-token', selections=[dict(line_token='pump_1', kind='existing', line_id=line.pk)]))
        for model in (CrmObjectNeedLine, CrmObjectNeedReviewLine):
            with self.assertRaises(ValidationError): model.objects.first().save()
            with self.assertRaises(ValidationError): model.objects.all().delete()

    def test_exact_replay_after_new_review_preserves_result_but_fresh_stale_request_fails(self):
        mapping = self.reconcile_payload()
        mapped = self.reconcile(mapping)
        payload = self.convert_payload()
        converted = self.convert(payload)
        # Latest review can omit a formerly selected position or change its mode.
        # Replaying a committed operation is not converting the new review.
        self.execute(operation_key='new-review-after-response-loss', positions=[
            dict(line_token='pump_1', description='Updated', mode='included', reason='New review')])
        self.assertEqual(self.reconcile(mapping), mapped)
        self.assertEqual(self.convert(payload), converted)
        with self.assertRaises(NeedConflict):
            self.convert(dict(payload, operation_key='fresh-stale-request'))
        with self.assertRaises((NeedConflict, CommercialConflict)):
            self.convert(dict(payload, title='Changed request with old key'))
        self.grant.edit_own_deals = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.reconcile(mapping)
        with self.assertRaises(PermissionDenied):
            self.convert(payload)
        self.assertEqual(CrmDeal.objects.count(), 1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(), 2)

    def test_alias_history_is_aggregated_without_materializing_old_bindings(self):
        from django.test.utils import CaptureQueriesContext
        self.reconcile(self.reconcile_payload(('pump_1',)))
        line = CrmObjectNeedLine.objects.get()
        for index in range(6):
            self.execute(operation_key=f'alias-review-{index}', positions=self.payload()['positions'])
            self.reconcile(dict(self.source(), operation_key=f'alias-map-{index}', selections=[
                dict(line_token='pump_1', kind='existing', line_id=line.pk)]))
        self.execute(operation_key='alias-final-review', positions=self.payload()['positions'])
        with CaptureQueriesContext(connection) as captured:
            self.reconcile(dict(self.source(), operation_key='alias-final-map', selections=[
                dict(line_token='pump_1', kind='existing', line_id=line.pk)]))
        queries = [row['sql'].upper() for row in captured.captured_queries]
        self.assertTrue(any('MIN(' in sql and 'MAX(' in sql and 'GROUP BY' in sql for sql in queries))
        self.assertEqual(CrmObjectNeedLine.objects.count(), 1)
        self.assertEqual(CrmObjectNeedReviewLine.objects.count(), 8)

    def test_forbidden_modes_tampered_client_ids_and_unsaved_identity_fail_closed(self):
        for mode in ('included','chemistry_monthly','warranty','error','cancelled','already_supplied'):
            self.execute(operation_key='mode-' + mode, positions=[dict(line_token='line_1', description='Private', mode=mode, reason='Checked')])
            with self.assertRaises(ValidationError): self.reconcile(self.reconcile_payload(('line_1',), 'map-' + mode))
        self.assertFalse(CrmObjectNeedLine.objects.exists())
        with self.assertRaises(ValidationError): self.reconcile(dict(self.source(), operation_key='bad-new-id', selections=[dict(line_token='line_1',kind='new',line_id=123)]))

    def test_failure_after_first_claim_rolls_back_entire_deal_creation(self):
        self.reconcile()
        before_commands = CrmCommand.objects.count()
        original = CrmObjectNeedDealLine.save
        calls = []
        def fail_last(instance, *args, **kwargs):
            calls.append(instance.line_id)
            if len(calls) == 2: raise ValidationError('Forced last claim failure')
            return original(instance, *args, **kwargs)
        with patch.object(CrmObjectNeedDealLine, 'save', fail_last):
            with self.assertRaises(ValidationError): self.convert()
        self.assertEqual(len(calls), 2)
        self.assertFalse(CrmDeal.objects.exists())
        self.assertFalse(CrmObjectNeedDealLine.objects.exists())
        self.assertFalse(CrmEvent.objects.exists())
        self.assertFalse(ServiceTask.objects.filter(task_type='crm_followup').exists())
        self.assertEqual(CrmCommand.objects.count(), before_commands)

    def test_reconcile_failure_rolls_back_new_identities_and_command(self):
        with patch.object(CrmObjectNeedReviewLine, 'save', side_effect=ValidationError('Forced binding failure')):
            with self.assertRaises(ValidationError): self.reconcile()
        self.assertFalse(CrmObjectNeedLine.objects.exists())
        self.assertFalse(CrmObjectNeedReviewLine.objects.exists())
        self.assertFalse(CrmCommand.objects.filter(command_type='object_need.reconcile').exists())

    def test_omitted_context_hash_compatibility_and_public_replay_cannot_bypass_source(self):
        self.reconcile()
        data = self.convert_payload()
        result = self.convert(data)
        generic = {key:data[key] for key in ('operation_key','client_id','pipeline_id','title','next_action')}
        generic['card_fields'] = {'object_kind':'pool','pool_id':self.pool.pk}
        with self.assertRaises(CommercialConflict): create_commercial_deal(actor=self.user, organization=self.org, **generic)
        generic['operation_key'] = 'ordinary-create'
        ordinary = create_commercial_deal(actor=self.user, organization=self.org, **generic)
        _, _, expected = _pilot_payload(actor=self.user, organization=self.org, operation_key='ordinary-create',
            command_type='deal.create', client_id=self.client_record.pk, pipeline_id=self.pipeline.pk,
            title=generic['title'], next_action=_next_action_values(generic['next_action']), card_fields=generic['card_fields'])
        self.assertEqual(CrmCommand.objects.get(operation_key='ordinary-create').payload_hash, expected)
        self.assertEqual(create_commercial_deal(actor=self.user, organization=self.org, **generic), ordinary)
        self.assertNotEqual(ordinary['deal_id'], result['deal_id'])
        with self.assertRaises(ValidationError): create_commercial_deal(actor=self.user, organization=self.org, **dict(generic, source_context={}))

    def test_current_capabilities_ownership_privacy_and_graph_on_fresh_and_replay(self):
        self.reconcile()
        payload = self.convert_payload()
        self.convert(payload)
        for name in ('view_team_deals','edit_own_deals','create_own_deals','manage_own_next_action'):
            setattr(self.grant, name, False); self.grant.save()
            with self.assertRaises(PermissionDenied): self.convert(payload)
            with self.assertRaises(PermissionDenied): self.convert(dict(payload, operation_key='after-revoke'))
            setattr(self.grant, name, True); self.grant.save()
        for model, pk, changes, restore in (
            (User,self.user.pk,{'is_active':False},{'is_active':True}),
            (Employee,self.employee.pk,{'is_active':False},{'is_active':True}),
            (Organization,self.org.pk,{'crm_v2_write':False},{'crm_v2_write':True}),
            (ServiceTask,self.task.pk,{'primary_responsible_id':None},{'primary_responsible_id':self.user.pk}),
            (ServiceTask,self.task.pk,{'payload_json':{'source_call_id':98765}},{'payload_json':{}}),
            (Pool,self.pool.pk,{'is_deleted':True},{'is_deleted':False}),
        ):
            model.objects.filter(pk=pk).update(**changes)
            with self.assertRaises((PermissionDenied,ValidationError)): self.convert(payload)
            model.objects.filter(pk=pk).update(**restore)
        with self.assertRaises(PermissionDenied): self.convert(dict(payload, client_id=self.client_record.pk + 999))
        self.assertEqual(CrmDeal.objects.count(), 1)

    def test_team_read_does_not_publish_source_ids_text_or_provenance_links(self):
        self.reconcile()
        result = self.convert()
        viewer = User.objects.create_user('team-only-reader')
        OrganizationAccess.objects.create(organization=self.org,user=viewer,role='manager')
        CrmAccess.objects.create(organization=self.org,user=viewer,view_team_deals=True)
        self.client.force_login(viewer)
        url = reverse('crm_live_detail',args=[result['deal_id']])
        response = self.client.get(url,{'org_id':self.org.pk})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['deal']['need_provenance'], {'restricted':True})
        self.assertNotIn('Scope and price',response.content.decode())
        lines = self.client.get(reverse('crm_object_need_lines',args=[self.task.pk]),{'org_id':self.org.pk})
        self.assertEqual(lines.status_code,403)
        self.client.force_login(self.user)
        provenance = self.client.get(url,{'org_id':self.org.pk}).json()['deal']['need_provenance']
        self.assertEqual(len(provenance['positions']),2)
        self.assertIn('need_task=',provenance['url'])
        self.task.responsibles.clear()
        ServiceTask.objects.filter(pk=self.task.pk).update(created_by=self.customer)
        hidden = self.client.get(url,{'org_id':self.org.pk}).json()['deal']['need_provenance']
        self.assertEqual(hidden, {'restricted':True})

    def test_consumed_sql_uniqueness_and_protect_are_real_constraints(self):
        self.reconcile()
        self.convert()
        claim = CrmObjectNeedDealLine.objects.first()
        historical = MigrationExecutor(connection).loader.project_state().apps.get_model('pool_service','CrmObjectNeedDealLine')
        values = CrmObjectNeedDealLine.objects.values().get(pk=claim.pk)
        values.pop('id')
        with self.assertRaises(IntegrityError), transaction.atomic(): historical.objects.create(**values)
        from django.db.models.deletion import ProtectedError
        with self.assertRaises(ProtectedError): claim.deal.delete()

    def test_http_strict_payload_and_bounded_history_search_after_authority(self):
        self.client.force_login(self.user)
        self.reconcile()
        self.convert()
        url = reverse('crm_object_need_lines',args=[self.task.pk])
        result = self.client.get(url,{'org_id':self.org.pk,'q':'pump_1'}).json()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['rows'][0]['description'],'Pump')
        payload = dict(self.convert_payload(),command_type='object_need.convert',source_context={})
        response = self.client.post(reverse('crm_object_need_deal_command') + '?org_id=' + str(self.org.pk),payload,content_type='application/json')
        self.assertEqual(response.status_code,400)
        self.grant.view_team_deals = False; self.grant.save()
        self.assertEqual(self.client.get(url,{'org_id':self.org.pk,'q':'pump_1'}).status_code,403)
