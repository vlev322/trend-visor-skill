import json
import unittest

from tools.model_eval.context_budget import default_margin, estimate_request_context
from tools.pageviews.errors import PageviewsError


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "system", "content": "Інструкції та references українською."},
            {"role": "user", "content": json.dumps({"state": "pinned", "daily_rows": []}, ensure_ascii=False)},
        ]
        self.tool = {
            "type": "function",
            "function": {
                "name": "wikipedia_workflow",
                "description": "Choose only an allowed action.",
                "parameters": {"type": "object", "properties": {"operation": {"type": "string"}}},
            },
        }

    def test_measures_complete_visible_payload_in_utf8_bytes_not_bytes_divided_by_four(self):
        expected = {
            "model": "model-id",
            "messages": self.messages,
            "temperature": 0,
            "max_tokens": 321,
            "tools": [self.tool],
            "tool_choice": "auto",
        }
        estimate = estimate_request_context(
            self.messages, self.tool, model="model-id", output_reserve_tokens=321,
        )
        expected_bytes = len(json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        self.assertEqual(estimate["input_utf8_bytes"], expected_bytes)
        self.assertEqual(estimate["input_token_upper_estimate"], expected_bytes)
        self.assertNotEqual(estimate["input_token_upper_estimate"], expected_bytes // 4)
        self.assertEqual(estimate["status"], "capacity_unverified")
        self.assertEqual(estimate["output_reserve_tokens"], 321)

    def test_context_window_includes_output_and_margin_with_inclusive_boundary(self):
        base = estimate_request_context(
            self.messages, self.tool, model="model-id", output_reserve_tokens=100,
        )
        margin = 256
        total = base["input_token_upper_estimate"] + 100 + margin
        fits = estimate_request_context(
            self.messages, self.tool, model="model-id", output_reserve_tokens=100,
            context_window_tokens=total, context_source="provider model docs checked by host",
            margin_tokens=margin,
        )
        too_large = estimate_request_context(
            self.messages, self.tool, model="model-id", output_reserve_tokens=100,
            context_window_tokens=total - 1, context_source="provider model docs checked by host",
            margin_tokens=margin,
        )
        self.assertEqual(fits["estimated_total_tokens"], total)
        self.assertEqual(fits["status"], "fits_declared_capacity_estimate")
        self.assertEqual(too_large["status"], "exceeds_declared_capacity_estimate")

    def test_soft_10000_guideline_never_rejects_a_larger_fitting_request(self):
        long_messages = [{"role": "user", "content": "Я" * 12000}]
        base = estimate_request_context(
            long_messages, None, model="model-id", output_reserve_tokens=100,
        )
        result = estimate_request_context(
            long_messages, None, model="model-id", output_reserve_tokens=100,
            context_window_tokens=50000, context_source="checked provider documentation",
        )
        self.assertTrue(base["soft_guideline_exceeded"])
        self.assertEqual(result["status"], "fits_declared_capacity_estimate")
        self.assertEqual(result["margin_tokens"], default_margin(50000))

    def test_default_margin_is_explicit_and_scaled(self):
        self.assertEqual(default_margin(150000), 7500)
        self.assertEqual(default_margin(1000), 256)
        self.assertEqual(default_margin(10**100), (10**100 * 5 + 99) // 100)

    def test_invalid_capacity_pairs_types_and_margins_are_structured(self):
        valid = {"model": "model-id", "output_reserve_tokens": 100}
        cases = (
            {**valid, "context_window_tokens": 150000},
            {**valid, "context_source": "docs"},
            {**valid, "context_window_tokens": True, "context_source": "docs"},
            {**valid, "context_window_tokens": 150000, "context_source": "docs", "margin_tokens": True},
            {**valid, "context_window_tokens": 150000, "context_source": "docs", "margin_tokens": 255},
            {**valid, "context_window_tokens": 150000, "context_source": "bad\nsource"},
            {**valid, "output_reserve_tokens": False},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(PageviewsError) as caught:
                estimate_request_context(self.messages, self.tool, **arguments)
            self.assertEqual(caught.exception.code, "invalid_request")
