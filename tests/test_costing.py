import unittest

from tools.model_eval.costing import summarize_cost, validate_tariff
from tools.pageviews.errors import PageviewsError


class CostingTests(unittest.TestCase):
    def setUp(self):
        self.tariff = {
            "input_per_million": "0.15",
            "output_per_million": "0.60",
            "currency": "USD",
            "tariff_date": "2026-09-01",
            "tariff_source": "Provider pricing page checked by host on 2026-09-26",
        }

    def test_cost_is_decimal_estimate_from_complete_usage_and_pinned_rate(self):
        usage = {"complete": True, "prompt_tokens": 100000, "completion_tokens": 10000}
        result = summarize_cost(usage, self.tariff)
        self.assertEqual(result["status"], "estimated_from_reported_usage")
        self.assertEqual(result["amount"], "0.02100000")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["tariff_date"], "2026-09-01")
        self.assertIn("not independently fetched", result["limitations"][1])

    def test_missing_usage_or_tariff_never_becomes_zero_cost(self):
        self.assertEqual(summarize_cost({"complete": False}, self.tariff)["status"], "unknown_usage_incomplete")
        unknown = summarize_cost({"complete": True, "prompt_tokens": 0, "completion_tokens": 0}, None)
        self.assertEqual(unknown["status"], "unknown_not_configured")
        self.assertIsNone(unknown["amount"])

    def test_tariff_requires_exact_fields_and_valid_date_currency_and_rates(self):
        self.assertEqual(validate_tariff(self.tariff)["input_per_million"], "0.15")
        invalid = (
            {**self.tariff, "input_per_million": "NaN"},
            {**self.tariff, "output_per_million": "-0.1"},
            {**self.tariff, "currency": "usd"},
            {**self.tariff, "tariff_date": "2026-02-30"},
            {**self.tariff, "tariff_source": ""},
            {key: value for key, value in self.tariff.items() if key != "tariff_source"},
        )
        for tariff in invalid:
            with self.subTest(tariff=tariff), self.assertRaises(PageviewsError):
                validate_tariff(tariff)
