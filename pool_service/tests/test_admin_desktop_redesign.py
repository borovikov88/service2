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
