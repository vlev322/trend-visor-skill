import json
import unittest
from copy import deepcopy

from tools.model_eval.cases import EvaluationCase
from tools.model_eval.runner import run_case


def reply(content=None, calls=None, finish="stop"):
    return {
        "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
        "finish_reason": finish, "model": "fixture-model", "id": "fixture-response",
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    }


def tool_reply(name="analyze_case", arguments='{"case_id":"missing_day"}'):
    return reply(calls=[{
        "id": "call-fixture", "type": "function",
        "function": {"name": name, "arguments": arguments},
    }], finish="tool_calls")


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, messages, tool):
        self.requests.append((deepcopy(messages), deepcopy(tool)))
        return next(self.responses)


class ModelEvaluationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "case_id": "missing_day", "observed_change_percent": None,
            "future_growth_established": False,
        }
        self.case = EvaluationCase(
            "missing_day", "Can missing days be assumed zero?",
            {"comparison": {"change_percent": None, "reason": "incomplete_coverage"}},
            self.expected,
        )

    def test_model_must_call_tool_and_ground_its_answer_in_the_result(self):
        model = ScriptedModel([tool_reply(), reply(json.dumps(self.expected))])
        result = run_case(self.case, model, "Methodology instructions")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["failed_fields"], [])
        messages, tool = model.requests[1]
        self.assertIsNone(tool)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "call-fixture")
        self.assertEqual(json.loads(messages[-1]["content"]), self.case.evidence)
        self.assertEqual(result["answer"], self.expected)
        self.assertEqual(result["question"], self.case.question)
        self.assertEqual(result["tool_schema"], model.requests[0][1])

    def test_correct_guess_without_tool_call_is_not_a_pass(self):
        model = ScriptedModel([reply(json.dumps(self.expected))])
        result = run_case(self.case, model, "Instructions")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "expected_tool_call")
        self.assertEqual(result["model_calls"], 1)

    def test_invented_percentage_or_guarantee_is_reported_not_repaired(self):
        answer = {**self.expected, "observed_change_percent": 25.0, "future_growth_established": True}
        model = ScriptedModel([tool_reply(), reply(json.dumps(answer))])
        result = run_case(self.case, model, "Instructions")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(set(result["failed_fields"]), {"observed_change_percent", "future_growth_established"})
        self.assertEqual(len(model.requests), 2)

    def test_unapproved_tools_and_different_cases_are_never_executed(self):
        for response in (
            tool_reply(name="run_shell"),
            tool_reply(arguments='{"case_id":"other"}'),
            tool_reply(arguments='{"case_id":"missing_day","path":".env"}'),
            tool_reply(arguments='{"case_id":"other","case_id":"missing_day"}'),
        ):
            with self.subTest(response=response):
                model = ScriptedModel([response])
                result = run_case(self.case, model, "Instructions")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], "unexpected_tool_call")
                self.assertEqual(len(model.requests), 1)

    def test_false_cannot_be_replaced_with_zero_and_nonfinite_json_is_rejected(self):
        for content in (
            json.dumps({**self.expected, "future_growth_established": 0}),
            '{"case_id":"missing_day","observed_change_percent":NaN,"future_growth_established":false}',
            '```json\n{}\n```',
        ):
            with self.subTest(content=content):
                model = ScriptedModel([tool_reply(), reply(content)])
                self.assertEqual(run_case(self.case, model, "Instructions")["status"], "failed")

    def test_truncated_response_is_a_failure_even_if_its_json_looks_complete(self):
        model = ScriptedModel([tool_reply(), reply(json.dumps(self.expected), finish="length")])
        result = run_case(self.case, model, "Instructions")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "incomplete_model_response")

    def test_numbers_allow_int_float_agreement_but_reject_huge_integers_safely(self):
        case = EvaluationCase("numeric", "Copy the computed value.", {"value": 1.0}, {"value": 1.0})
        for value, status in ((1, "passed"), (1.0, "passed"), (10**400, "failed")):
            with self.subTest(status=status, type=type(value).__name__):
                model = ScriptedModel([
                    tool_reply(arguments='{"case_id":"numeric"}'), reply(json.dumps({"value": value})),
                ])
                result = run_case(case, model, "Instructions")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["model_calls"], 2)

    def test_oversized_or_deeply_nested_answers_fail_within_the_call_budget(self):
        for content in (" " * 10001, "[" * 1100 + "]" * 1100):
            model = ScriptedModel([tool_reply(), reply(content)])
            result = run_case(self.case, model, "Instructions")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "invalid_answer_json")
            self.assertEqual(result["model_calls"], 2)