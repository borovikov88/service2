from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from pool_service.services.ai_costs import (
    PRICE_TABLE,
    PRICING_VERSION,
    calculate_usage_cost,
    cost_context,
    usage_record,
)


class AICostTests(SimpleTestCase):
    USAGE = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 200_000,
        "output_tokens": 100_000,
    }

    def test_price_table_is_exact_and_versioned(self):
        self.assertEqual(PRICING_VERSION, "openai-2026-08-10")
        self.assertEqual(
            set(PRICE_TABLE),
            {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"},
        )

    def test_gpt_5_6_alias_uses_sol_pricing(self):
        self.assertEqual(PRICE_TABLE["gpt-5.6"], PRICE_TABLE["gpt-5.6-sol"])
        self.assertEqual(
            calculate_usage_cost("gpt-5.6", **self.USAGE),
            Decimal("7.10000000"),
        )

    def test_models_price_input_cached_input_and_output_exactly(self):
        expected_rates = {
            "gpt-5.6-sol": ("5.00000000", "0.50000000", "30.00000000"),
            "gpt-5.6-terra": ("2.50000000", "0.25000000", "15.00000000"),
            "gpt-5.6-luna": ("1.00000000", "0.10000000", "6.00000000"),
        }
        for model, (input_cost, cached_cost, output_cost) in expected_rates.items():
            with self.subTest(model=model, token_type="input"):
                self.assertEqual(
                    calculate_usage_cost(model, 1_000_000, 0, 0),
                    Decimal(input_cost),
                )
            with self.subTest(model=model, token_type="cached_input"):
                self.assertEqual(
                    calculate_usage_cost(model, 1_000_000, 1_000_000, 0),
                    Decimal(cached_cost),
                )
            with self.subTest(model=model, token_type="output"):
                self.assertEqual(
                    calculate_usage_cost(model, 0, 0, 1_000_000),
                    Decimal(output_cost),
                )

    def test_cached_input_is_subtracted_from_normal_input(self):
        expected = {
            "gpt-5.6-sol": "7.10000000",
            "gpt-5.6-terra": "3.55000000",
            "gpt-5.6-luna": "1.42000000",
        }
        for model, amount in expected.items():
            with self.subTest(model=model):
                self.assertEqual(
                    calculate_usage_cost(model, **self.USAGE),
                    Decimal(amount),
                )

    def test_cached_input_greater_than_input_is_unknown(self):
        self.assertIsNone(calculate_usage_cost("gpt-5.6-sol", 10, 11, 1))

    def test_unknown_model_does_not_become_zero_cost(self):
        record = usage_record(
            SimpleNamespace(
                model="unlisted-model",
                usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            )
        )
        self.assertIsNone(record["calculated_cost_usd"])
        self.assertIsNone(record["pricing_version"])

    def test_unknown_snapshot_like_model_is_not_aliased(self):
        self.assertIsNone(
            calculate_usage_cost("gpt-5.6-2026-08-10", **self.USAGE)
        )

    def test_usage_record_saves_pricing_version_only_for_known_cost(self):
        known = usage_record(
            SimpleNamespace(
                model="gpt-5.6-sol",
                usage=SimpleNamespace(
                    input_tokens=1_000_000,
                    input_tokens_details=SimpleNamespace(cached_tokens=200_000),
                    output_tokens=100_000,
                ),
            )
        )
        unknown = usage_record(
            SimpleNamespace(
                model="gpt-5.6-unknown",
                usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            )
        )
        self.assertEqual(known["calculated_cost_usd"], "7.10000000")
        self.assertEqual(known["pricing_version"], PRICING_VERSION)
        self.assertIsNone(unknown["calculated_cost_usd"])
        self.assertIsNone(unknown["pricing_version"])

    def test_historical_saved_cost_is_not_recalculated(self):
        iteration = SimpleNamespace(
            automation_metadata={
                "ai_usage": {
                    "stage": "primary_analysis",
                    "calls": [
                        {
                            "model": "retired-model",
                            "calculated_cost_usd": "1.23450000",
                            "pricing_version": "historical-version",
                        }
                    ],
                }
            }
        )
        context = cost_context([iteration])
        self.assertEqual(context["analysis"]["amount"], Decimal("1.23450000"))
        self.assertEqual(context["total"], Decimal("1.23450000"))
