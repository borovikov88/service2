"""Run against local MySQL; deliberately skipped on SQLite, which has no row locks."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.test import Client as Browser, TransactionTestCase
from django.urls import reverse

from pool_service.client_crm_import import apply_candidate
from pool_service.client_crm_models import ClientImportCandidate, ClientImportProposal
from pool_service.client_import_review import decide, token
from pool_service.models import Client, Organization
from .test_client_import_review import ReviewFixture


@skipUnless(connection.vendor == "mysql", "Requires MySQL/InnoDB row locks")
class OnecReviewConcurrencyTests(ReviewFixture, TransactionTestCase):
    def worker(self, function):
        connections.close_all()
        try:
            return function()
        finally:
            connections.close_all()

    def test_manual_edit_wins_before_import_fills_empty_field(self):
        Client.objects.filter(pk=self.customer.pk).update(phone="")
        started=threading.Event()
        def sync():
            started.set();return apply_candidate(self.candidate).pk
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                client=Client.objects.select_for_update().get(pk=self.customer.pk)
                future=pool.submit(self.worker,sync)
                self.assertTrue(started.wait(3));self.assertFalse(future.done())
                client.phone="+12025550188";client.save(update_fields=["phone"])
            future.result(timeout=15)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone,"+12025550188")
        self.assertEqual(self.proposal().before,"+12025550188")

    def test_import_wins_then_waiting_full_editor_rejects_stale_form(self):
        browser=Browser();browser.force_login(self.actor)
        url=reverse("client_edit",args=[self.customer.pk])
        rev=browser.get(url).context["editor_revision"]
        started=threading.Event()
        def edit():
            started.set()
            return browser.post(url,{"name":"Synthetic stale manual name","editor_revision":rev}).status_code
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                apply_candidate(self.candidate) # fills empty first/last fields
                future=pool.submit(self.worker,edit)
                self.assertTrue(started.wait(3));self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=15),409)
        self.customer.refresh_from_db();self.assertEqual(self.customer.name,"Synthetic Old")

    def test_simultaneous_accepts_are_idempotent(self):
        apply_candidate(self.candidate);row=self.proposal();barrier=threading.Barrier(2)
        def accept():
            barrier.wait(timeout=5)
            return self.decision(row).state
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.worker,accept) for _ in range(2)]
            self.assertEqual([f.result(timeout=15) for f in futures],["accepted","accepted"])
        row.refresh_from_db();self.assertEqual(row.state,"accepted")
        self.assertEqual(ClientImportProposal.objects.filter(candidate=self.candidate,field="phone").count(),1)

    def test_sync_source_change_prevents_waiting_accept(self):
        apply_candidate(self.candidate);row=self.proposal();started=threading.Event()
        def accept():
            started.set();return self.decision(row).state
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                future=pool.submit(self.worker,accept);self.assertTrue(started.wait(3))
                ClientImportCandidate.objects.filter(pk=self.candidate.pk).update(phone="+12025550177")
            self.assertEqual(future.result(timeout=15),"stale")
        self.customer.refresh_from_db();self.assertEqual(self.customer.phone,"+12025550101")

    def test_concurrent_imports_make_one_proposal_per_field_version(self):
        barrier=threading.Barrier(2)
        def sync():
            barrier.wait(timeout=5);return apply_candidate(self.candidate).pk
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.worker,sync) for _ in range(2)]
            self.assertEqual([f.result(timeout=15) for f in futures],[self.customer.pk,self.customer.pk])
        self.assertEqual(ClientImportProposal.objects.filter(candidate=self.candidate,field="phone").count(),1)
