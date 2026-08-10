from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from pool_service.services.ai_costs import (
    CODEX_ESTIMATOR_VERSION,
    LONG_CONTEXT_THRESHOLD,
    PRICE_TABLE,
    PRICING_VERSION,
    calculate_usage_cost,
    codex_cost_estimate,
    cost_context,
    usage_record,
)


class AICostTests(SimpleTestCase):
    USAGE = {
        "input_tokens": 200_000,
        "cached_input_tokens": 20_000,
        "output_tokens": 100_000,
    }

    def test_price_table_is_exact_and_versioned(self):
        self.assertEqual(PRICING_VERSION, "openai-2026-08-10-long-context-v2")
        self.assertEqual(LONG_CONTEXT_THRESHOLD, 272_000)
        self.assertEqual(
            set(PRICE_TABLE),
            {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"},
        )

    def test_codex_forecast_is_deterministic_for_complexity_and_model(self):
        expected = {
            ("simple", "gpt-5.6-luna"): ("0.04400000", "0.20000000"),
            ("standard", "gpt-5.6-terra"): ("0.27500000", "1.40000000"),
            ("complex", "gpt-5.6-sol"): ("1.10000000", "9.40000000"),
        }
        for (complexity, model), amounts in expected.items():
            with self.subTest(complexity=complexity, model=model):
                first = codex_cost_estimate(complexity, model)
                self.assertEqual(first, codex_cost_estimate(complexity, model))
                self.assertEqual((first["min_usd"], first["max_usd"]), amounts)
                self.assertEqual(first["estimator_version"], CODEX_ESTIMATOR_VERSION)
                self.assertEqual(first["pricing_version"], PRICING_VERSION)

    def test_codex_forecast_supports_every_complexity_model_combination(self):
        for complexity in ("simple", "standard", "complex"):
            for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
                with self.subTest(complexity=complexity, model=model):
                    estimate = codex_cost_estimate(complexity, model)
                    self.assertEqual(estimate["complexity"], complexity)
                    self.assertEqual(estimate["model"], model)

    def test_codex_forecast_fails_closed_for_unknown_inputs(self):
        self.assertIsNone(codex_cost_estimate("unknown", "gpt-5.6-sol"))
        self.assertIsNone(codex_cost_estimate("simple", "unknown"))

    def test_gpt_5_6_alias_uses_sol_pricing(self):
        self.assertEqual(PRICE_TABLE["gpt-5.6"], PRICE_TABLE["gpt-5.6-sol"])
        self.assertEqual(
            calculate_usage_cost("gpt-5.6", **self.USAGE),
            Decimal("3.91000000"),
        )

    def test_models_price_input_cached_input_and_output_exactly(self):
        expected_rates = {
            "gpt-5.6-sol": ("1.00000000", "0.10000000", "30.00000000"),
            "gpt-5.6-terra": ("0.50000000", "0.05000000", "15.00000000"),
            "gpt-5.6-luna": ("0.20000000", "0.02000000", "6.00000000"),
        }
        for model, (input_cost, cached_cost, output_cost) in expected_rates.items():
            with self.subTest(model=model, token_type="input"):
                self.assertEqual(
                    calculate_usage_cost(model, 200_000, 0, 0),
                    Decimal(input_cost),
                )
            with self.subTest(model=model, token_type="cached_input"):
                self.assertEqual(
                    calculate_usage_cost(model, 200_000, 200_000, 0),
                    Decimal(cached_cost),
                )
            with self.subTest(model=model, token_type="output"):
                self.assertEqual(
                    calculate_usage_cost(model, 0, 0, 1_000_000),
                    Decimal(output_cost),
                )

    def test_cached_input_is_subtracted_from_normal_input(self):
        expected = {
            "gpt-5.6-sol": "3.91000000",
            "gpt-5.6-terra": "1.95500000",
            "gpt-5.6-luna": "0.78200000",
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
                    input_tokens=200_000,
                    input_tokens_details=SimpleNamespace(cached_tokens=20_000),
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
        self.assertEqual(known["calculated_cost_usd"], "3.91000000")
        self.assertEqual(known["pricing_version"], PRICING_VERSION)
        self.assertIsNone(unknown["calculated_cost_usd"])
        self.assertIsNone(unknown["pricing_version"])

    def test_long_context_boundary_for_each_model(self):
        expected_at_threshold = {
            "gpt-5.6-sol": "4.36000000",
            "gpt-5.6-terra": "2.18000000",
            "gpt-5.6-luna": "0.87200000",
        }
        expected_above_threshold = {
            "gpt-5.6-sol": "7.22001000",
            "gpt-5.6-terra": "3.61000500",
            "gpt-5.6-luna": "1.44400200",
        }
        for model in expected_at_threshold:
            with self.subTest(model=model, input_tokens=LONG_CONTEXT_THRESHOLD):
                self.assertEqual(
                    calculate_usage_cost(model, LONG_CONTEXT_THRESHOLD, 0, 100_000),
                    Decimal(expected_at_threshold[model]),
                )
            with self.subTest(model=model, input_tokens=LONG_CONTEXT_THRESHOLD + 1):
                self.assertEqual(
                    calculate_usage_cost(model, LONG_CONTEXT_THRESHOLD + 1, 0, 100_000),
                    Decimal(expected_above_threshold[model]),
                )

    def test_long_context_output_uses_one_point_five_multiplier(self):
        expected_output_costs = {
            "gpt-5.6-sol": "45.00000000",
            "gpt-5.6-terra": "22.50000000",
            "gpt-5.6-luna": "9.00000000",
        }
        for model, expected_output in expected_output_costs.items():
            without_output = calculate_usage_cost(
                model, LONG_CONTEXT_THRESHOLD + 1, 0, 0
            )
            with_output = calculate_usage_cost(
                model, LONG_CONTEXT_THRESHOLD + 1, 0, 1_000_000
            )
            with self.subTest(model=model):
                self.assertEqual(with_output - without_output, Decimal(expected_output))

    def test_long_context_with_cached_input_fails_closed(self):
        for model in PRICE_TABLE:
            with self.subTest(model=model):
                self.assertIsNone(
                    calculate_usage_cost(model, LONG_CONTEXT_THRESHOLD + 1, 1, 0)
                )

        record = usage_record(
            SimpleNamespace(
                model="gpt-5.6-sol",
                usage=SimpleNamespace(
                    input_tokens=LONG_CONTEXT_THRESHOLD + 1,
                    input_tokens_details=SimpleNamespace(cached_tokens=1),
                    output_tokens=0,
                ),
            )
        )
        self.assertIsNone(record["calculated_cost_usd"])
        self.assertIsNone(record["pricing_version"])

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
