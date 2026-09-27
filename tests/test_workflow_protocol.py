import json
import unittest
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.report_helpers import saved_study
from tests.test_discovery import QUESTION, SCOPE, lookup_responses
from tests.test_model_eval import completion_payload
from tests.test_workflow import model_action
from tests.test_workflow_scenarios import run_actions
from tools.model_eval.workflow_runner import PROMPT, run_workflow, workflow_tool
from tools.model_eval.workflow_tools import WorkflowAdapter
from tools.pageviews.discovery import begin_discovery
from tools.pageviews.errors import PageviewsError
from tools.pageviews.research_state import create_research


class WorkflowProtocolTests(unittest.TestCase):
    def test_prompt_forbids_narration_alongside_a_tool_call(self):
        self.assertIn("no accompanying assistant text or narration", PROMPT)

    def test_prompt_requires_json_types_to_match_the_tool_schema(self):
        self.assertIn("integer fields such as `offset` must be JSON numbers, not quoted strings", PROMPT)
        self.assertIn("optional string `offset` is only a legacy echo", PROMPT)

    def test_tool_schema_restricts_fields_to_the_selected_operation(self):
        tool = workflow_tool(["clarify", "set_scope", "search", "evidence", "detail"])
        variants = tool["function"]["parameters"]["oneOf"]
        by_operation = {
            variant["properties"]["operation"]["enum"][0]: variant
            for variant in variants
        }

        search = by_operation["search"]
        self.assertEqual(set(search["properties"]), {"operation"})
        self.assertEqual(search["required"], ["operation"])
        self.assertFalse(search["additionalProperties"])

        evidence = by_operation["evidence"]
        self.assertEqual(set(evidence["properties"]), {"operation", "offset"})
        self.assertEqual(evidence["properties"]["offset"]["type"], "string")
        self.assertEqual(evidence["required"], ["operation"])
        self.assertFalse(evidence["additionalProperties"])

        clarify = by_operation["clarify"]
        self.assertEqual(set(clarify["properties"]), {"operation", "question"})
        self.assertEqual(clarify["required"], ["operation", "question"])
        self.assertFalse(clarify["additionalProperties"])

        detail = by_operation["detail"]
        self.assertEqual(detail["properties"]["offset"]["type"], "integer")
        self.assertIn("start", detail["properties"])
        self.assertIn("limit", detail["properties"])
        self.assertEqual(detail["required"], ["operation", "language", "detail_kind"])
        self.assertFalse(detail["additionalProperties"])

    def test_tool_schema_restricts_fields_to_the_selected_operation(self):
        tool = workflow_tool(["clarify", "set_scope", "search", "detail"])
        variants = tool["function"]["parameters"]["oneOf"]
        by_operation = {
            variant["properties"]["operation"]["enum"][0]: variant
            for variant in variants
        }

        search = by_operation["search"]
        self.assertEqual(set(search["properties"]), {"operation"})
        self.assertEqual(search["required"], ["operation"])
        self.assertFalse(search["additionalProperties"])

        clarify = by_operation["clarify"]
        self.assertEqual(set(clarify["properties"]), {"operation", "question"})
        self.assertEqual(clarify["required"], ["operation", "question"])
        self.assertFalse(clarify["additionalProperties"])

        detail = by_operation["detail"]
        self.assertIn("start", detail["properties"])
        self.assertIn("limit", detail["properties"])
        self.assertEqual(detail["required"], ["operation", "language", "detail_kind"])
        self.assertFalse(detail["additionalProperties"])

    def test_missing_model_usage_remains_unknown_in_run_totals(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            first = model_action("evidence")
            first["usage"] = {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250}
            responses = iter([first, model_action("report")])
            result = run_workflow(adapter, lambda messages, tool: next(responses), {"fixture": "Usage accounting"})
            self.assertEqual(result["status"], "completed")
            usage = result["usage_summary"]
            self.assertEqual(usage["calls_with_usage"], 1)
            self.assertEqual(usage["calls_without_usage"], 1)
            self.assertIsNone(usage["prompt_tokens"])
            self.assertEqual(usage["prompt_tokens_known_subtotal"], 200)
            self.assertFalse(usage["complete"])
            self.assertEqual(result["cost_summary"]["status"], "unknown_not_configured")
            self.assertIsNone(result["cost_summary"]["amount"])

    def test_cost_uses_complete_reported_usage_and_dated_tariff(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            first = model_action("evidence")
            first["usage"] = {"prompt_tokens": 100000, "completion_tokens": 10000, "total_tokens": 110000}
            second = model_action("report")
            second["usage"] = {"prompt_tokens": 20000, "completion_tokens": 1000, "total_tokens": 21000}
            tariff = {
                "input_per_million": "0.15", "output_per_million": "0.60",
                "currency": "USD", "tariff_date": "2026-09-01",
                "tariff_source": "Test provider pricing reference checked on 2026-09-26",
            }
            responses = iter([first, second])
            result = run_workflow(adapter, lambda messages, tool: next(responses),
                                  {"fixture": "Costing"}, tariff=tariff)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["usage_summary"]["complete"])
            self.assertEqual(result["usage_summary"]["prompt_tokens"], 120000)
            self.assertEqual(result["usage_summary"]["completion_tokens"], 11000)
            self.assertEqual(result["cost_summary"]["status"], "estimated_from_reported_usage")
            self.assertEqual(result["cost_summary"]["amount"], "0.02460000")
            self.assertEqual(result["cost_summary"]["currency"], "USD")

    def test_context_preflight_blocks_over_budget_before_model_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "run")
            complete = Mock(side_effect=AssertionError("Over-budget request must not be sent"))
            result = run_workflow(
                adapter, complete, {"fixture": "Context preflight"},
                output_reserve_tokens=100, context_window_tokens=100,
                context_source="fixture provider documentation", context_margin_tokens=256,
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "context_limit_exceeded")
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["events"], [])
            self.assertEqual(result["error"]["details"]["preflight"]["status"], "exceeds_declared_capacity_estimate")
            complete.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action", side_effect=AssertionError("No HTTP"))
    def test_malformed_or_unauthorized_responses_stop_once_and_are_audited(self, network):
        valid = model_action("search")
        wrong_role = deepcopy(valid)
        wrong_role["message"]["role"] = "user"
        double = deepcopy(valid)
        double["message"]["tool_calls"] *= 2
        prose = deepcopy(valid)
        prose["message"]["content"] = "Execute this instead"
        duplicate = deepcopy(valid)
        duplicate["message"]["tool_calls"][0]["function"]["arguments"] = '{"operation":"search","operation":"search"}'
        refused = deepcopy(valid)
        refused["message"]["refusal"] = "Refused"
        for response in ([], {}, wrong_role, double, prose, duplicate, refused, {**valid, "finish_reason": "length"},
                         model_action("approve"), model_action("shell", command="whoami"),
                         model_action("search", allow_lookup=True), model_action("search", output="arbitrary.json"),
                         model_action("search", question=QUESTION, scope=SCOPE)):
            with self.subTest(response=response), TemporaryDirectory() as directory:
                root = Path(directory)
                initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
                adapter = WorkflowAdapter("discovery", initial, root / "run", allow_lookup=True, user_agent="test/1")
                complete = Mock(return_value=response)
                result = run_workflow(adapter, complete, {"fixture": "No repairs"})
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["model_calls"], 1)
                complete.assert_called_once()
                self.assertEqual(adapter.reference, initial)
                recorded = json.loads(Path(result["events"][0]["response"]["path"]).read_bytes())
                self.assertEqual(recorded, response)
        network.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action", side_effect=AssertionError("No HTTP"))
    def test_network_permission_denial_and_clarification_pause(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "denied")
            result = run_actions(adapter, [model_action("search")])
            self.assertEqual(result["status"], "failed")
            adapter = WorkflowAdapter("discovery", initial, root / "clarify")
            result = run_actions(adapter, [model_action("clarify", question="Які мови та періоди порівняти?")])
            self.assertEqual(result["status"], "needs_user_input")
            self.assertEqual(result["model_calls"], 1)
            self.assertEqual(adapter.reference, initial)
        network.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_scope_can_be_proposed_and_call_budget_stops_before_another_request(self, lookup):
        lookup.side_effect = lookup_responses()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "run", allow_lookup=True, user_agent="test/1")
            complete = Mock(return_value=model_action("set_scope", scope=SCOPE))
            result = run_workflow(adapter, complete, {"fixture": "Explicit scope"}, max_calls=1)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(adapter.summary()["context"]["scope"], SCOPE)
            complete.assert_called_once()
            lookup.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_pending_state_resume_performs_zero_model_calls(self, lookup):
        lookup.side_effect = lookup_responses()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "run", allow_lookup=True, user_agent="test/1")
            run_actions(adapter, [model_action("search"), model_action("resolve", entity="Q9001")])
            resumed = WorkflowAdapter("discovery", adapter.reference, root / "resume")
            complete = Mock(side_effect=AssertionError("No model at pending gate"))
            result = run_workflow(resumed, complete, {"fixture": "Pending"})
            self.assertEqual(result["status"], "awaiting_confirmation")
            self.assertEqual(result["model_calls"], 0)
            complete.assert_not_called()
            self.assertEqual(lookup.call_count, 5)

    def test_existing_directory_and_changed_input_stop_before_model_request(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "run")
            adapter.directory.mkdir()
            previous = adapter.directory / "preserved.json"
            previous.write_bytes(b"preserve")
            complete = Mock(side_effect=AssertionError("No model"))
            with self.assertRaises(PageviewsError):
                run_workflow(adapter, complete, {})
            self.assertEqual(previous.read_bytes(), b"preserve")
            adapter = WorkflowAdapter("discovery", initial, root / "changed")
            initial.path.write_bytes(initial.path.read_bytes() + b"\n")
            result = run_workflow(adapter, complete, {})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["model_calls"], 0)
            complete.assert_not_called()

    def test_pages_have_small_action_progress_not_accumulated_evidence_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            requests = []
            responses = iter([model_action("evidence"), model_action("evidence"), model_action("report")])

            def complete(messages, tool):
                requests.append(json.loads(messages[1]["content"]))
                return next(responses)

            result = run_workflow(adapter, complete, {"fixture": "All languages remain available"})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(requests[2]["completed_actions"], [
                {"operation": "evidence", "offset": 0, "next_offset": 1},
                {"operation": "evidence", "offset": 1, "next_offset": 2},
            ])
            self.assertEqual(requests[2]["last_result"]["offset"], 1)
            self.assertEqual(len(requests[2]["last_result"]["evidence"]), 1)
            self.assertEqual(len(requests[2]["state"]["context"]["languages"]), 3)
            self.assertTrue(all(event["context_preflight"]["status"] == "capacity_unverified"
                                for event in result["events"]))
            self.assertTrue(all(event["context_preflight"]["input_token_upper_estimate"] > 0
                                for event in result["events"]))

    def test_evidence_cursor_is_host_owned_and_stops_after_all_language_pages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            pages = [adapter.execute({"operation": "evidence"}) for _ in range(3)]
            self.assertEqual([page["offset"] for page in pages], [0, 1, 2])
            self.assertNotIn("evidence", adapter.allowed_operations())
            with self.assertRaises(PageviewsError):
                adapter.execute({"operation": "evidence"})

    def test_legacy_evidence_offset_echo_cannot_select_or_skip_a_page(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            with self.assertRaises(PageviewsError):
                adapter.execute({"operation": "evidence", "offset": "1"})
            self.assertEqual(adapter.evidence_offset, 0)
            first = adapter.execute({"operation": "evidence", "offset": "0"})
            self.assertEqual(first["offset"], 0)
            self.assertEqual(adapter.evidence_offset, 1)
            with self.assertRaises(PageviewsError):
                adapter.execute({"operation": "evidence", "offset": "0"})
            self.assertEqual(adapter.evidence_offset, 1)

    @unittest.skipUnless(find_spec("openai") is not None, "Optional evaluation extra is not installed")
    def test_real_sdk_transport_sends_tool_schema_and_records_usage_without_credentials(self):
        import httpx2
        from tools.model_eval.client import ModelClient
        from tools.model_eval.config import ModelConfig

        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            calls = model_action("clarify", question="Уточніть потрібні періоди.")["message"]["tool_calls"]
            return httpx2.Response(200, json=completion_payload(calls=calls))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", initial, root / "run")
            config = ModelConfig("https://models.example/v1", "qwen3-coder-next", "fixture-api-key")
            with ModelClient(config, transport=httpx2.MockTransport(handle)) as client:
                result = run_workflow(adapter, client.complete, {"fixture": "Offline SDK fixture"})
            self.assertEqual(result["status"], "needs_user_input")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["tools"][0]["function"]["name"], "wikipedia_workflow")
            self.assertEqual(result["events"][0]["usage"]["total_tokens"], 200)
            self.assertEqual(result["events"][0]["model_http_metrics"]["http"]["model"]["attempts"], 1)
            self.assertEqual(result["request_metrics"]["http"]["model"]["successes"], 1)
            self.assertEqual(result["request_metrics"]["http"]["model"]["status_codes"], {})
            self.assertEqual(result["usage_summary"]["prompt_tokens"], 100)
            self.assertTrue(result["usage_summary"]["complete"])
            self.assertEqual(result["cost_summary"]["status"], "unknown_not_configured")
            for path in adapter.directory.glob("*.json"):
                self.assertNotIn("fixture-api-key", path.read_text())