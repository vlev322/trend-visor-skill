import json
import hashlib
import io
import unittest
from email.message import Message
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from tests.report_helpers import saved_study
from tests.test_discovery import QUESTION, SCOPE, lookup_responses, views_response
from tests.test_workflow import model_action
from tools.model_eval.workflow_runner import load_instructions, run_workflow
from tools.model_eval.workflow_tools import WorkflowAdapter
from tools.pageviews.discovery import approve_discovery, begin_discovery
from tools.pageviews.errors import PageviewsError
from tools.pageviews.research_state import approve_criteria, create_research, read_research, research_report

CONDITION = "Описова зміна щонайменше 150%"
RULE = {"id": "change", "description": CONDITION, "metric": "change_percent", "operator": "gte", "threshold": 150, "parameters": {}}


def run_actions(adapter, actions, captured_requests=None, instructions=None):
    responses = iter(actions)

    def complete(messages, tool):
        if captured_requests is not None:
            captured_requests.append(messages)
        return next(responses)

    instructions = instructions or {"fixture": "Synthetic model decisions, not human consent"}
    return run_workflow(adapter, complete, instructions)


class WorkflowScenarioTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Clarification must not fetch"))
    def test_unsupported_causal_demand_question_can_pause_without_proxy_report(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            question = "Чи спричиняє зростання переглядів готовність купувати курс?"
            initial = create_research(study, question=question, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            result = run_actions(adapter, [model_action(
                "clarify", question="Вам потрібне описове порівняння переглядів чи окреме дослідження готовності платити?",
            )])
            self.assertEqual(result["status"], "needs_user_input")
            self.assertEqual(result["model_calls"], 1)
            self.assertNotIn("report_artifacts", result)
            self.assertEqual(adapter.reference, initial)
        network.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_partial_collection_audit_keeps_successful_and_failed_http_attempts(self, lookup):
        lookup.side_effect = lookup_responses()
        responses = iter([
            views_response("cs", "Městské_zahradničení", (10, 20, 30, 60)),
            HTTPError("https://api.wikimedia.org", 429, "Fixture limit", Message(), io.BytesIO(b"")),
        ])

        def pageview_response(request, **kwargs):
            response = next(responses)
            if isinstance(response, HTTPError):
                raise response
            return response

        with patch("tools.pageviews.client.urlopen", side_effect=pageview_response), TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            discover = WorkflowAdapter("discovery", initial, root / "discovery-run",
                                       allow_lookup=True, user_agent="test/1")
            run_actions(discover, [model_action("search"), model_action("resolve", entity="Q9001")])
            approved = approve_discovery(
                discover.reference, confirmation=discover.reference.sha256,
                user_reply="Synthetic test approval only.", output=root / "approved.json",
            )
            adapter = WorkflowAdapter("discovery", approved, root / "collect-run",
                                       allow_collection=True, user_agent="test/1", cache_dir=root / "cache")
            result = run_actions(adapter, [model_action("collect")])
            self.assertEqual(result["status"], "collection_incomplete")
            pageviews = result["request_metrics"]["http"]["pageviews"]
            self.assertEqual(pageviews["attempts"], 2)
            self.assertEqual(pageviews["successes"], 1)
            self.assertEqual(pageviews["errors"], 1)
            self.assertEqual(pageviews["status_codes"], {"200": 1, "429": 1})
            self.assertEqual(lookup.call_count, 5)
            metadata = result["request_metrics"]["http"]["metadata"]
            self.assertEqual(metadata["attempts"], 0)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_model_reads_two_detail_pages_then_reports_all_languages_without_state_change(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40), "pl": (5, 15, 25, 35)}, unmatched=("en",))
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            self.assertIn("detail", adapter.allowed_operations())
            requests = []
            responses = iter([
                model_action("detail", language="cs", detail_kind="observations",
                             start="2026-07-01", end="2026-07-04", offset=0, limit=2),
                model_action("detail", language="cs", detail_kind="observations",
                             start="2026-07-01", end="2026-07-04", offset=2, limit=2),
                model_action("report"),
            ])

            def complete(messages, tool):
                requests.append((json.loads(messages[1]["content"]), tool))
                return next(responses)

            result = run_workflow(adapter, complete, {"fixture": "Synthetic model decisions, not human consent"})
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertEqual(adapter.reference, initial)
            self.assertEqual(len(requests), 3)
            operations = [
                branch["properties"]["operation"]["enum"][0]
                for branch in requests[0][1]["function"]["parameters"]["oneOf"]
            ]
            self.assertIn("detail", operations)
            self.assertEqual(requests[2][0]["last_result"]["kind"], "observations")
            self.assertEqual(requests[2][0]["last_result"]["page"]["offset"], 2)
            self.assertEqual(len(requests[2][0]["last_result"]["items"]), 2)
            self.assertEqual(requests[2][0]["completed_actions"], [
                {"operation": "detail", "language": "cs", "offset": 0,
                 "detail_kind": "observations", "start": "2026-07-01", "end": "2026-07-04",
                 "limit": 2, "next_offset": 2, "total": 4},
                {"operation": "detail", "language": "cs", "offset": 2,
                 "detail_kind": "observations", "start": "2026-07-01", "end": "2026-07-04",
                 "limit": 2, "next_offset": None, "total": 4},
            ])
            report = json.loads(Path(result["report_artifacts"]["report"]).read_bytes())
            self.assertEqual([row["language"] for row in report["evidence"]], ["cs", "pl", "en"])
            self.assertEqual(report["verification"]["network_requests"], 0)
            detail_artifacts = [json.loads(Path(event["result"]["path"]).read_bytes())
                                for event in result["events"][:2]]
            self.assertEqual([item["page"]["offset"] for item in detail_artifacts], [0, 2])
        network.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    @patch("tools.pageviews.topics.fetch_action")
    def test_independent_topic_crosses_both_host_gates_and_renders_verified_report(self, lookup, network):
        lookup.side_effect = lookup_responses()
        network.side_effect = [views_response("cs", "Městské_zahradničení", (10, 20, 30, 60)),
                               views_response("pl", "Ogrodnictwo_miejskie", (100, 200, 200, 400))]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, criteria=[CONDITION], output=root / "initial.json")
            discover = WorkflowAdapter("discovery", initial, root / "discovery-run", allow_lookup=True, user_agent="test/1")
            result = run_actions(discover, [model_action("search"), model_action("resolve", entity="Q9001")])
            self.assertEqual(result["status"], "awaiting_confirmation")
            network.assert_not_called()
            approved = approve_discovery(discover.reference, confirmation=discover.reference.sha256,
                                          user_reply="Синтетична згода для тесту, не рішення користувача.", output=root / "approved-discovery.json")
            research = WorkflowAdapter("discovery", approved, root / "research-run", allow_collection=True,
                                       user_agent="test/1", cache_dir=root / "cache")
            captured = []
            result = run_actions(research, [model_action("collect"), model_action("evidence"),
                                            model_action("analyze", language="cs", top_days=1, trim_days=1),
                                            model_action("analyze", language="pl", top_days=1, trim_days=1),
                                            model_action("propose_rules", rules=[RULE], match="all")], captured,
                               lambda current: load_instructions(Path(__file__).resolve().parents[1], current))
            self.assertEqual(result["status"], "awaiting_confirmation", result.get("error"))
            self.assertIn("references/discovery-workflow.md", captured[0][0]["content"])
            self.assertNotIn("references/report-criteria.md", captured[0][0]["content"])
            self.assertIn("references/research-state.md", captured[1][0]["content"])
            self.assertIn("references/evidence-details.md", captured[1][0]["content"])
            self.assertIn("references/report-criteria.md", captured[-1][0]["content"])
            self.assertIn("references/discovery-workflow.md", result["events"][0]["instruction_sha256"])
            self.assertNotIn("references/report-criteria.md", result["events"][0]["instruction_sha256"])
            self.assertIn("references/research-state.md", result["events"][1]["instruction_sha256"])
            self.assertIn("references/evidence-details.md", result["events"][1]["instruction_sha256"])
            loaded_research_ref = Path(__file__).resolve().parents[1] / "references/research-state.md"
            self.assertEqual(result["events"][1]["instruction_sha256"]["references/research-state.md"],
                             hashlib.sha256(loaded_research_ref.read_bytes()).hexdigest())
            self.assertEqual(result["events"][1]["instruction_utf8_bytes"]["references/research-state.md"],
                             loaded_research_ref.stat().st_size)
            collection_metrics = result["request_metrics"]
            collection_usage = result["usage_summary"]
            pending = read_research(research.reference)
            self.assertEqual(len(pending["inputs"]["analyses"]), 2)
            rules = json.loads(Path(pending["rules"]["path"]).read_bytes())
            self.assertEqual(rules["rules"][0]["description"], CONDITION)
            self.assertIsNone(pending["approval"])
            with self.assertRaises(PageviewsError):
                research_report(research.reference)
            approved = approve_criteria(research.reference, confirmation=research.reference.sha256,
                                         user_reply="Синтетична згода з тестовим порогом.", output=root / "approved-research.json")
            final = WorkflowAdapter("research", approved, root / "final-run")
            self.assertNotIn("analyze", final.allowed_operations())
            self.assertNotIn("chart", final.allowed_operations())
            result = run_actions(final, [model_action("evidence"), model_action("report")])
            self.assertEqual(result["status"], "completed", result.get("error"))
            report = json.loads(Path(result["report_artifacts"]["report"]).read_bytes())
            self.assertEqual([row["analysis"]["comparison"]["change_percent"] for row in report["evidence"]], [200, 100])
            self.assertEqual([row["criteria_evaluation"]["status"] for row in report["evidence"]], ["matches", "does_not_match"])
            self.assertEqual(report["verification"]["diagnostics"], "recomputed_and_matched")
            self.assertEqual(report["question"], QUESTION)
            self.assertEqual(lookup.call_count, 5)
            self.assertEqual(network.call_count, 2)
            self.assertEqual(collection_metrics["http"]["pageviews"]["attempts"], 2)
            self.assertEqual(collection_metrics["http"]["pageviews"]["successes"], 2)
            self.assertEqual(collection_metrics["cache"]["misses"], 2)
            self.assertEqual(collection_metrics["http"]["metadata"]["attempts"], 0)
            self.assertIsNone(collection_usage["prompt_tokens"])
            self.assertEqual(collection_usage["calls_without_usage"], 5)

    @unittest.skipUnless(find_spec("matplotlib") is not None, "Optional charts extra is not installed")
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_chart_is_created_from_active_snapshot_and_attached_before_report(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            result = run_actions(adapter, [model_action("chart", language="cs"), model_action("report")])
            self.assertEqual(result["status"], "completed", result.get("error"))
            report = json.loads(Path(result["report_artifacts"]["report"]).read_bytes())
            self.assertEqual(report["verification"]["charts"], "checksum_metadata_and_source_matched")
            self.assertTrue(Path(report["evidence"][0]["chart"]["path"]).is_file())
            self.assertTrue(report["evidence"][0]["chart"]["visual_review_required"])
            network.assert_not_called()