from django.template.loader import get_template
from django.test import SimpleTestCase


class FinanceDesktopRedesignTests(SimpleTestCase):
    templates = [
        "pool_service/finance/overview.html",
        "pool_service/finance/dashboard.html",
        "pool_service/finance/onec_profit_dashboard.html",
        "pool_service/finance/onec_cashflow_dashboard.html",
        "pool_service/finance/payroll_dashboard.html",
    ]

    def test_primary_finance_templates_compile_with_desktop_workspace(self):
        for template_name in self.templates:
            with self.subTest(template=template_name):
                template = get_template(template_name)
                source = template.template.source
                self.assertIn("finance-desktop", source)
                self.assertIn(
                    'pool_service/finance/_desktop_workspace_styles.html',
                    source,
                )

    def test_owner_overview_uses_compact_wide_screen_cards(self):
        template = get_template("pool_service/finance/overview.html")
        source = template.template.source

        self.assertIn("finance-owner-overview", source)
        self.assertIn("finance-kpi-card", source)
        self.assertIn("finance-data-card", source)

    def test_management_finance_reports_use_compact_filters_and_cards(self):
        for template_name in [
            "pool_service/finance/onec_profit_dashboard.html",
            "pool_service/finance/onec_cashflow_dashboard.html",
            "pool_service/finance/payroll_dashboard.html",
        ]:
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                self.assertIn("finance-filter-card", source)
                self.assertIn("finance-data-card", source)
