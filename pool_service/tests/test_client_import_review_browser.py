"""Explicit local Chromium UI verification, never contacts production/1C/CDNs."""
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from django.db import connections
from unittest import skipUnless

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from pool_service.client_crm_import import apply_candidate
from .test_client_import_review import ReviewFixture


@skipUnless(os.environ.get("ONEC_REVIEW_BROWSER_TEST") == "1", "Explicit local browser opt-in")
class OnecReviewBrowserTests(ReviewFixture, StaticLiveServerTestCase):
    def test_review_and_editor_on_desktop_and_mobile(self):
        from playwright.sync_api import sync_playwright
        apply_candidate(self.candidate)
        self.client.force_login(self.actor)
        proposal_id = self.proposal().pk
        def db_read(callback):
            def run():
                try:
                    return callback()
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(run).result(timeout=10)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
            page=browser.new_page(viewport={"width":1366,"height":900})
            def submit(button):
                with page.expect_navigation(wait_until="load"):
                    button.click()
            page.route("**/*",lambda route: route.continue_() if route.request.url.startswith(self.live_server_url) else route.abort())
            page.context.add_cookies([{"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url}])
            url=self.live_server_url+reverse("client_onec_proposals")
            page.goto(url)
            row=page.locator('[data-proposal="%s"]'%proposal_id)
            self.assertTrue(row.get_by_text("Основной телефон",exact=False).is_visible())
            submit(row.get_by_role("button",name="Отклонить",exact=True))
            self.assertEqual(db_read(lambda: self.proposal().state),"rejected")
            page.get_by_role("link",name="Отклонено",exact=True).click()
            self.assertTrue(page.locator('[data-proposal="%s"]'%proposal_id).is_visible())
            page.set_viewport_size({"width":390,"height":844});page.goto(url)
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),392)
            Path("/tmp/onec-review-evidence").mkdir(exist_ok=True)
            page.screenshot(path="/tmp/onec-review-evidence/mobile.png",full_page=True)
            page.set_viewport_size({"width":1366,"height":900});page.goto(url)
            page.screenshot(path="/tmp/onec-review-evidence/desktop.png",full_page=True)
            email_id=db_read(lambda: self.proposal("email").pk)
            submit(page.locator('[data-proposal="%s"]'%email_id).get_by_role("button",name="Принять",exact=True))
            self.assertEqual(db_read(lambda: self.proposal("email").state),"accepted")
            from pool_service.models import Client
            name_id=db_read(lambda: self.proposal("name").pk)
            db_read(lambda: Client.objects.filter(pk=self.customer.pk).update(name="Synthetic Concurrent Edit"))
            submit(page.locator('[data-proposal="%s"]'%name_id).get_by_role("button",name="Принять",exact=True))
            self.assertEqual(db_read(lambda: self.proposal("name").state),"stale")
            page.get_by_role("link",name="Устарело",exact=True).click()
            submit(page.locator('[data-proposal="%s"]'%name_id).get_by_role("button",name="Обновить сравнение",exact=True))
            submit(page.locator('[data-proposal="%s"]'%name_id).get_by_role("button",name="Принять",exact=True))
            self.assertEqual(db_read(lambda: self.proposal("name").state),"accepted")
            page.goto(self.live_server_url+reverse("client_edit",args=[self.customer.pk]))
            self.assertEqual(page.locator("form form").count(),0)
            page.locator('[data-crm-editor] input[name="name"]').fill("Synthetic Browser Edit")
            submit(page.get_by_role("button",name="Сохранить изменения",exact=True))
            from pool_service.models import Client
            self.assertEqual(db_read(lambda: Client.objects.get(pk=self.customer.pk).name),"Synthetic Browser Edit")
            browser.close()
