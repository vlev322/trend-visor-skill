import json
import unittest
from copy import deepcopy

from tests.final_answer_helpers import final_answer, final_operations, model_fields
from tools.model_eval.final_answer import (
    answer_facts, final_messages, response_format, validate_answer, validate_response,
)
from tools.pageviews.errors import PageviewsError


class FinalAnswerTests(unittest.TestCase):
    def test_valid_answer_keeps_processing_coverage_and_calendar_scope_separate(self):
        operations = final_operations()
        before = deepcopy(operations)
        expected = final_answer()
        self.assertEqual(answer_facts(operations), expected["facts"])
        result = validate_answer(json.dumps(model_fields(expected)), operations)
        self.assertEqual(result["facts"], expected["facts"])
        self.assertIn("зросли з 10,00 до 20,00 на день", result["summary_uk"])
        self.assertEqual(operations, before)

    def test_fenced_json_remains_rejected_instead_of_repaired(self):
        text = "```json\n" + json.dumps(model_fields(final_answer())) + "\n```"
        with self.assertRaises(PageviewsError) as caught:
            validate_answer(text, final_operations())
        self.assertEqual(caught.exception.code, "live_invalid_answer")

    def test_wrong_processing_or_calendar_claim_fails_even_as_valid_json(self):
        for field, value in (
            ("collection_status", "complete"), ("current_coverage_status", "partial"),
            ("descriptive_seasonality_adjusted", True), ("trend_calendar_controls", []),
            ("trend_interval_status", "computed"), ("trend_interval_reason", None),
            ("current_missing_days", False), ("current_missing_days", 0.0000005),
            ("change_percent", 101.0),
        ):
            with self.subTest(field=field):
                answer = final_answer()
                answer["facts"][0][field] = value
                with self.assertRaises(PageviewsError) as caught:
                    validate_answer(json.dumps(model_fields(answer)), final_operations())
                self.assertEqual(caught.exception.code, "live_fact_mismatch")
                self.assertIn(f"uk:{field}", caught.exception.details["fields"])

    def test_incomplete_and_unresolved_rows_retain_nulls_and_reasons(self):
        operations = final_operations()
        row = operations["study_fresh"]["results"][0]
        row["analysis"]["current"].update(status="partial")
        row["analysis"]["current"]["coverage"]["missing_days"] = 1
        row["analysis"]["comparison"].update(change_percent=None, reason="incomplete_coverage")
        row["analysis"]["methodology"]["trend_model"].update(
            status="not_computed", reason="incomplete_coverage",
        )
        row["analysis"]["methodology"]["trend_model"]["confidence_interval"]["reason"] = "model_not_fitted"
        operations["study_fresh"]["results"].append({
            "language": "pl", "status": "not_collected", "resolution_status": "no_sitelink", "reason": "no_sitelink",
        })
        facts = answer_facts(operations)
        self.assertEqual([row["language"] for row in facts], ["uk", "pl"])
        self.assertEqual(facts[0]["collection_status"], "analyzed")
        self.assertEqual(facts[0]["current_coverage_status"], "partial")
        self.assertIsNone(facts[0]["change_percent"])
        self.assertIsNone(facts[1]["current_missing_days"])
        self.assertEqual(facts[1]["change_reason"], "no_sitelink")
        self.assertIsNone(facts[1]["trend_interval_status"])
        self.assertEqual(facts[1]["trend_calendar_controls"], [])

    def test_schema_carries_types_not_the_expected_answers(self):
        schema = response_format()["json_schema"]
        self.assertTrue(schema["strict"])
        root = schema["schema"]
        self.assertFalse(root["additionalProperties"])
        self.assertEqual(set(root["required"]), set(root["properties"]))
        item = root["properties"]["facts"]["items"]
        self.assertEqual(set(item["required"]), set(model_fields(final_answer())["facts"][0]))
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertNotIn("source_url", item["properties"])
        self.assertNotIn("chart_path", item["properties"])
        self.assertNotIn("complete", item["properties"]["collection_status"]["enum"])
        self.assertIn("complete", item["properties"]["current_coverage_status"]["enum"])
        self.assertNotIn("const", json.dumps(schema))
        self.assertNotIn("Synthetic article", json.dumps(schema))

    def test_final_messages_use_evidence_not_the_previous_model_answer(self):
        scope = {"question": "Чи зростає інтерес?", "languages": ["uk"]}
        messages = final_messages(scope, final_operations(), "FULL SKILL INSTRUCTIONS")
        self.assertEqual(len(messages), 2)
        self.assertIn("FULL SKILL INSTRUCTIONS", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["scope"], scope)
        evidence = payload["articles"][0]
        self.assertEqual(evidence["processing_status"], "analyzed")
        self.assertEqual(evidence["baseline"]["status"], "complete")
        self.assertEqual(evidence["trend_model"]["parameters"]["calendar_controls"], ["weekday", "month_of_year"])
        self.assertNotIn("expected", payload)
        self.assertNotIn("answer", payload)
        self.assertNotIn("/tmp/synthetic-chart.png", messages[1]["content"])

    def test_host_binds_exact_artifact_links_but_rejects_model_supplied_paths(self):
        wanted = final_answer()
        content = json.dumps(model_fields(wanted))
        answer = validate_answer(content, final_operations())
        self.assertEqual(answer["facts"], wanted["facts"])
        self.assertNotIn("source_url", json.loads(content)["facts"][0])
        self.assertNotIn("chart_path", json.loads(content)["facts"][0])
        for field in ("source_url", "chart_path"):
            supplied = model_fields(wanted)
            supplied["facts"][0][field] = "invented-link"
            with self.assertRaises(PageviewsError) as caught:
                validate_answer(json.dumps(supplied), final_operations())
            self.assertEqual(caught.exception.code, "live_fact_mismatch")

    def test_unapproved_text_checks_and_fields_are_rejected(self):
        for field, value in (("extra", True), ("summary_uk", "Зростання гарантовано"), ("interpretations", []),
                             ("next_checks", []), ("next_checks", [4]), ("next_checks", ["other_languages"]),
                             ("next_checks", ["review_page_history", "review_page_history"])):
            with self.subTest(field=field):
                answer = model_fields(final_answer())
                answer[field] = value
                with self.assertRaises(PageviewsError):
                    validate_answer(json.dumps(answer), final_operations())

    def test_wrong_interpretation_is_rejected_not_corrected_by_rendering(self):
        for field, value in (("period_change_direction", "decrease"), ("trend_interval_available", True),
                             ("trend_interval_available", 0)):
            with self.subTest(field=field):
                answer = model_fields(final_answer())
                answer["interpretations"][0][field] = value
                with self.assertRaises(PageviewsError) as caught:
                    validate_answer(json.dumps(answer), final_operations())
                self.assertEqual(caught.exception.code, "live_interpretation_mismatch")

    def test_truncation_refusal_and_tool_calls_are_not_accepted_as_a_final_answer(self):
        response = {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(model_fields(final_answer()))}}
        self.assertEqual(validate_response(response, final_operations())["facts"], final_answer()["facts"])
        for variant in (
            {**response, "finish_reason": "length"},
            {**response, "message": {**response["message"], "refusal": "Refused"}},
            {**response, "message": {**response["message"], "tool_calls": [{"id": "unexpected"}]}},
            {**response, "message": {"content": None}},
            {**response, "message": {"content": " "}},
            [],
        ):
            with self.assertRaises(PageviewsError):
                validate_response(variant, final_operations())