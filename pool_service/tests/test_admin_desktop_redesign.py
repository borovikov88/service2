from django.template.loader import get_template
from django.test import SimpleTestCase


class AdminDesktopRedesignTests(SimpleTestCase):
    templates = [
        "pool_service/clients.html",
        "pool_service/client_create.html",
        "pool_service/users.html",
        "pool_service/organization_norms.html",
        "pool_service/profile.html",
        "pool_service/billing.html",
        "pool_service/pool_create.html",
        "pool_service/archive.html",
        "pool_service/notifications.html",
        "pool_service/client_staff.html",
        "pool_service/client_invite_create.html",
        "pool_service/invite_create.html",
        "pool_service/billing_admin.html",
        "pool_service/pool_service_details.html",
        "pool_service/water_reading_form.html",
        "pool_service/development/iteration_form.html",
        "pool_service/finance_mcp/authorize.html",
        "pool_service/onec_diagnostic_mcp/authorize.html",
        "pool_service/development/task_list.html",
        "pool_service/development/task_form.html",
        "pool_service/development/task_detail.html",
    ]

    def test_admin_templates_compile_with_desktop_workspace(self):
        for template_name in self.templates:
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                self.assertIn("admin-desktop", source)
                self.assertIn(
                    'pool_service/_admin_workspace_styles.html',
                    source,
                )

    def test_shared_admin_workspace_has_compact_classes(self):
        source = get_template(
            "pool_service/_admin_workspace_styles.html"
        ).template.source
        self.assertIn("admin-page-header", source)
        self.assertIn("admin-card", source)
        self.assertIn("admin-form", source)

    def test_visual_qa_desktop_density_regressions(self):
        reading = get_template("pool_service/water_reading_form.html").template.source
        self.assertIn("reading-entry-form", reading)
        self.assertIn('height: 88px', reading)

        development = get_template(
            "pool_service/development/task_detail.html"
        ).template.source
        self.assertIn("development-state-form", development)
        self.assertIn("align-items-start", development)
        self.assertIn("height: 72px", development)

        notifications = get_template(
            "pool_service/notifications.html"
        ).template.source
        self.assertIn("max-width: 1360px", notifications)

