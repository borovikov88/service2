"""Actual row-lock gates; SQLite skips are not MySQL evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCRMProfile
from pool_service.client_merge import merge_clients
from pool_service.services.contact_roles import set_supplier


@skipUnlessDBFeature('has_select_for_update')
class ContactsConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.org=Organization.objects.create(name="Synthetic lock org",paid_until=timezone.now()+timedelta(days=30))
        self.actor=get_user_model().objects.create_user(username="synthetic-lock-owner")
        OrganizationAccess.objects.create(user=self.actor,organization=self.org,role="owner")
        self.source=Client.objects.create(name="Synthetic source",organization=self.org,client_type="legal")
        self.target=Client.objects.create(name="Synthetic target",organization=self.org,client_type="legal")
        ClientCRMProfile.objects.create(client=self.target,onec_ref="00000000-0000-0000-0000-000000000567")

    def threaded(self, action):
        close_old_connections()
        try:return action()
        finally:connections['default'].close()

    def attempt(self):
        try:set_supplier(self.actor,self.source.pk,"1",0);return "ok"
        except (PermissionDenied,ValidationError):return "denied"

    def test_competing_writes_one_revision_one_audit(self):
        barrier=Barrier(2)
        def run():barrier.wait(timeout=10);return self.attempt()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(self.threaded,run) for _ in range(2)]
            results=[f.result(timeout=20) for f in futures]
        self.assertCountEqual(results,["ok","denied"])
        self.assertEqual(ClientBusinessRole.objects.count(),1)
        self.assertEqual(ClientBusinessRoleEvent.objects.count(),1)

    def test_held_merge_organization_blocks_role_then_rechecks_alias(self):
        held,release,started,done=Event(),Event(),Event(),Event()
        def merger():
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                held.set()
                if not release.wait(10):raise RuntimeError("Release timeout")
                merge_clients(self.source.pk,self.target.pk,organization_id=self.org.pk,actor=self.actor)
        def writer():
            started.set()
            try:return self.attempt()
            finally:done.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(self.threaded,merger)
            self.assertTrue(held.wait(10))
            second=executor.submit(self.threaded,writer)
            try:
                self.assertTrue(started.wait(10));self.assertFalse(done.wait(.2))
            finally:release.set()
            first.result(timeout=20);self.assertEqual(second.result(timeout=20),"denied")
        self.assertFalse(ClientBusinessRoleEvent.objects.exists())
