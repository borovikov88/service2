from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.client_crm_models import ClientContact, OrganizationPersonLink
from pool_service.services.contact_people import save_phone, phone_revision, link_person


@skipUnlessDBFeature('has_select_for_update')
class ContactPeopleConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic',paid_until=timezone.now()+timedelta(days=30))
        self.actor=get_user_model().objects.create_user(username='synthetic-owner')
        OrganizationAccess.objects.create(user=self.actor,organization=self.org,role='owner')
        self.person=Client.objects.create(name='Synthetic person',organization=self.org,client_type='private')

    def race(self, action):
        barrier=Barrier(2)
        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:action();return 'ok'
                except ValidationError:return 'stale'
            finally:connections['default'].close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(run) for _ in range(2)]
            return [f.result(timeout=20) for f in futures]

    def test_phone_one_current_revision(self):
        revision=phone_revision(self.person)
        self.assertCountEqual(self.race(lambda:save_phone(self.actor,self.person.pk,value='+79001112233',phone_type='work',primary='1',revision=revision)),['ok','stale'])
        self.assertEqual(ClientContact.objects.count(),1)

    def test_link_replay_has_one_row(self):
        self.assertEqual(self.race(lambda:link_person(self.actor,self.person.pk,self.actor.pk)),['ok','ok'])
        self.assertEqual(OrganizationPersonLink.objects.count(),1)
