from pathlib import Path

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from pool_service.theme import THEME_COOKIE_MAX_AGE, THEME_COOKIE_NAME


class ThemePreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="theme-user", password="pass")
        self.client.force_login(self.user)

    def test_profile_defaults_to_auto_theme(self):
        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-preference="auto"')
        self.assertContains(response, 'name="theme" value="auto" checked')
        self.assertContains(response, "assets/css/theme.css")

    def test_theme_preference_is_saved_in_device_cookie(self):
        response = self.client.post(
            reverse("profile"),
            {
                "theme_settings": "1",
                "theme": "dark",
            },
        )

        self.assertEqual(response.status_code, 302)
        cookie = response.cookies[THEME_COOKIE_NAME]
        self.assertEqual(cookie.value, "dark")
        self.assertEqual(int(cookie["max-age"]), THEME_COOKIE_MAX_AGE)
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertTrue(cookie["httponly"])

        follow_up = self.client.get(reverse("profile"))
        self.assertContains(follow_up, 'data-theme-preference="dark"')
        self.assertContains(follow_up, 'name="theme" value="dark" checked')

        secure_response = self.client.post(
            reverse("profile"),
            {
                "theme_settings": "1",
                "theme": "light",
            },
            secure=True,
        )
        self.assertTrue(secure_response.cookies[THEME_COOKIE_NAME]["secure"])

    def test_invalid_theme_is_rejected_without_setting_cookie(self):
        response = self.client.post(
            reverse("profile"),
            {
                "theme_settings": "1",
                "theme": "neon",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(THEME_COOKIE_NAME, response.cookies)

    def test_dark_theme_has_readable_finance_and_warning_overrides(self):
        css_path = finders.find("assets/css/theme.css")
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text(encoding="utf-8")

        self.assertIn('html[data-theme="dark"] .alert-warning', css)
        self.assertIn('html[data-theme="dark"] .owner-dashboard', css)
        self.assertIn('html[data-theme="dark"] .owner-badge.preliminary', css)
        self.assertIn('html[data-theme="dark"] .finance-desktop .finance-data-card thead th', css)
        self.assertIn('html[data-theme="dark"] .app-logo', css)
        self.assertIn('background: #101a28;', css)
        self.assertNotIn('filter: brightness(0) invert(1);', css)
        self.assertIn('html[data-theme="dark"] .multi-select__trigger', css)
        self.assertIn('html[data-theme="dark"] .multi-select__dropdown', css)
        self.assertIn('html[data-theme="dark"] .multi-select__option:has(input:checked)', css)
        self.assertIn('html[data-theme="dark"] .expense-employee-cell__last', css)
        self.assertIn('Full dark-theme audit: shared residual components.', css)
        self.assertIn('html[data-theme="dark"] .photo-picker-sheet__panel', css)
        self.assertIn('html[data-theme="dark"] .task-history', css)


    def test_calendar_has_dark_theme_overrides(self):
        template_path = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "pool_service"
            / "readings_all.html"
        )
        template = template_path.read_text(encoding="utf-8-sig")

        self.assertIn('html[data-theme="dark"] .calendar-toolbar', template)
        self.assertIn('html[data-theme="dark"] .calendar-weekday', template)
        self.assertIn('html[data-theme="dark"] .calendar-day', template)
        self.assertIn('html[data-theme="dark"] .calendar-chip--planned', template)
        self.assertIn('html[data-theme="dark"] .calendar-list-section', template)


    def test_notifications_and_cash_have_dark_theme_overrides(self):
        templates_root = Path(__file__).resolve().parents[1] / "templates" / "pool_service"
        notifications = (templates_root / "notifications.html").read_text(encoding="utf-8")
        cash = (templates_root / "finance" / "cash_dashboard.html").read_text(encoding="utf-8")

        self.assertIn('html[data-theme="dark"] .tg-panel', notifications)
        self.assertIn('html[data-theme="dark"] .tg-item.is-unread', notifications)
        self.assertIn('html[data-theme="dark"] .kkm-metric--primary', cash)
        self.assertIn('html[data-theme="dark"] .kkm-history-card tbody tr:nth-child(even)', cash)


    def test_full_dark_theme_audit_covers_remaining_workspaces(self):
        templates_root = Path(__file__).resolve().parents[1] / "templates" / "pool_service"
        pool_list = (templates_root / "pool_list.html").read_text(encoding="utf-8")
        pool_detail = (templates_root / "pool_detail.html").read_text(encoding="utf-8")
        task_view = (templates_root / "task_view_body.html").read_text(encoding="utf-8")
        archive = (templates_root / "archive.html").read_text(encoding="utf-8")
        conversations = (templates_root / "communications" / "conversations.html").read_text(encoding="utf-8")
        unlock = (templates_root / "security" / "unlock.html").read_text(encoding="utf-8")
        cash_count = (templates_root / "finance" / "cash_count_form.html").read_text(encoding="utf-8")

        self.assertIn('html[data-theme="dark"] .pool-controls', pool_list)
        self.assertIn('html[data-theme="dark"] .object-role-strip', pool_detail)
        self.assertIn('html[data-theme="dark"] .task-view__hero', task_view)
        self.assertIn('html[data-theme="dark"] .archive-card', archive)
        self.assertIn('html[data-theme="dark"] .communications-grid', conversations)
        self.assertIn('html[data-theme="dark"] .unlock-screen', unlock)
        self.assertIn('html[data-theme="dark"] .cash-count-meta__box', cash_count)

    def test_finance_modal_forms_follow_theme_background(self):
        templates_root = Path(__file__).resolve().parents[1] / "templates" / "pool_service" / "finance"
        for name in ("card_transfer_form.html", "expense_form.html", "income_form.html", "transaction_form.html"):
            template = (templates_root / name).read_text(encoding="utf-8")
            self.assertIn("background: var(--bs-body-bg)", template)
            self.assertNotIn("body { background: #fff; }", template)
