from types import SimpleNamespace
from decimal import Decimal

from django.test import SimpleTestCase

from pool_service.services.ai_costs import (
    PRICING_VERSION,
    calculate_usage_cost,
    usage_record,
)


class AICostTests(SimpleTestCase):
    def test_cached_input_is_priced_separately(self):
        amount = calculate_usage_cost("gpt-4.1", 1000, 200, 300)
        expected = (Decimal("800") * Decimal("2.00") + Decimal("200") * Decimal("0.50") + Decimal("300") * Decimal("8.00")) / Decimal("1000000")
        self.assertEqual(amount, expected.quantize(Decimal("0.00000001")))

    def test_usage_record_uses_provider_usage_and_fixed_pricing_version(self):
        response = SimpleNamespace(
            model="gpt-4.1",
            usage=SimpleNamespace(
                input_tokens=1000,
                input_tokens_details=SimpleNamespace(cached_tokens=200),
                output_tokens=300,
            ),
        )
        record = usage_record(response)
        self.assertEqual(record["input_tokens"], 1000)
        self.assertEqual(record["cached_input_tokens"], 200)
        self.assertEqual(record["output_tokens"], 300)
        self.assertEqual(record["pricing_version"], PRICING_VERSION)
        self.assertEqual(record["calculated_cost_usd"], "0.00410000")

    def test_unknown_model_does_not_become_zero_cost(self):
        record = usage_record(SimpleNamespace(
            model="unlisted-model",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        ))
        self.assertIsNone(record["calculated_cost_usd"])
        self.assertIsNone(record["pricing_version"])
