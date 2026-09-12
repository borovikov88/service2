from django.test import TestCase

from pool_service.finance_imports.finance_position import get_finance_position
from pool_service.models import Organization


class MissingFinancePositionTests(TestCase):
    def test_missing_snapshot_is_unavailable_not_zero(self):
        organization = Organization.objects.create(name="No balance snapshot")

        result = get_finance_position(organization)

        self.assertFalse(result["available"])
        self.assertEqual(result["freshness"]["status"], "missing")
        for field in (
            "cash_regular",
            "cash_kkm",
            "cash_in_transit",
            "cash_total",
            "receivables",
            "customer_advances",
            "payables",
            "supplier_advances",
            "sign_anomaly_count",
            "sign_anomaly_amount",
            "calculated_position",
        ):
            self.assertIsNone(result[field])
