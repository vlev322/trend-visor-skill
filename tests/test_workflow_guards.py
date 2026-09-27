import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_study
from tests.test_report_criteria import QUESTION as CRITERIA_QUESTION, saved_rules
from tests.test_discovery import QUESTION, SCOPE, lookup_responses
from tests.test_workflow import model_action
from tests.test_workflow_scenarios import CONDITION, RULE, run_actions
from tools.model_eval.workflow_tools import WorkflowAdapter
from tools.pageviews.discovery import approve_discovery, begin_discovery
from tools.pageviews.errors import PageviewsError
from tools.pageviews.research_state import approve_criteria, create_research, propose_criteria, read_research


class WorkflowGuardTests(unittest.TestCase):
    def test_detail_is_read_only_and_model_cannot_override_pinned_state_or_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            initial = create_research(study, question=CRITERIA_QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            result = adapter.execute({
                "operation": "detail", "language": "cs", "detail_kind": "observations",
                "start": "2026-07-01", "end": "2026-07-02", "offset": 0, "limit": 1,
            })
            self.assertEqual(result["items"][0]["views"], 10)
            self.assertEqual(adapter.reference, initial)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            for injected in (
                {"path": "/tmp/other-state.json"},
                {"url": "https://example.invalid"},
                {"approval": True},
                {"operation": "shell"},
            ):
                with self.subTest(injected=injected), self.assertRaises(PageviewsError):
                    adapter.execute({
                        "operation": "detail", "language": "cs", "detail_kind": "observations",
                        **injected,
                    })

    def test_pending_research_pauses_before_model_can_read_detail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=CRITERIA_QUESTION, output=root / "research.json")
            rules, _ = saved_rules(root, study, [RULE])
            pending = propose_criteria(initial, rules, output=root / "pending.json")
            adapter = WorkflowAdapter("research", pending, root / "run")
            result = run_actions(adapter, [model_action("detail", language="cs", detail_kind="observations",
                                                        start="2026-07-01", end="2026-07-02")])
            self.assertEqual(result["status"], "awaiting_confirmation")
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(adapter.reference, pending)

    def test_approved_detail_read_preserves_exact_approval_record(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=CRITERIA_QUESTION, output=root / "research.json")
            rules, _ = saved_rules(root, study)
            pending = propose_criteria(initial, rules, output=root / "pending.json")
            approved = approve_criteria(
                pending, confirmation=pending.sha256, user_reply="Synthetic approval fixture.",
                output=root / "approved.json",
            )
            adapter = WorkflowAdapter("research", approved, root / "run")
            saved_approval = read_research(approved)["approval"]
            result = adapter.execute({
                "operation": "detail", "language": "cs", "detail_kind": "observations",
                "start": "2026-07-01", "end": "2026-07-02",
            })
            self.assertEqual(result["status"], "available")
            self.assertEqual(adapter.reference, approved)
            self.assertEqual(read_research(approved)["approval"], saved_approval)

    def test_invalid_rule_proposal_does_not_clear_original_unresolved_conditions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, criteria=[CONDITION], output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            result = run_actions(adapter, [model_action("propose_rules", rules=[{**RULE, "metric": "imagined_metric"}], match="all")])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(adapter.reference, initial)
            self.assertEqual(read_research(adapter.reference)["inputs"]["criteria"], [CONDITION])

    def test_lossy_threshold_in_original_model_arguments_is_not_silently_rounded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            initial = create_research(study, question=QUESTION, output=root / "research.json")
            adapter = WorkflowAdapter("research", initial, root / "run")
            response = model_action("propose_rules", rules=[RULE], match="all")
            function = response["message"]["tool_calls"][0]["function"]
            function["arguments"] = function["arguments"].replace('"threshold": 150', '"threshold": 1.00000000000000001')
            result = run_actions(adapter, [response])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "invalid_workflow_response")
            self.assertEqual(adapter.reference, initial)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Offline means no HTTP"))
    @patch("tools.pageviews.topics.fetch_action")
    def test_collection_failures_pause_with_preserved_handoff_not_success(self, lookup, network):
        lookup.side_effect = lookup_responses()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            discover = WorkflowAdapter("discovery", initial, root / "discovery", allow_lookup=True, user_agent="test/1")
            run_actions(discover, [model_action("search"), model_action("resolve", entity="Q9001")])
            approved = approve_discovery(discover.reference, confirmation=discover.reference.sha256,
                                          user_reply="Synthetic test approval only.", output=root / "approved.json")
            adapter = WorkflowAdapter("discovery", approved, root / "collect", cache_dir=root / "empty-cache")
            result = run_actions(adapter, [model_action("collect")])
            self.assertEqual(result["status"], "collection_incomplete", result.get("error"))
            self.assertEqual(result["model_calls"], 1)
            self.assertEqual(result["active_state"]["kind"], "research")
            self.assertEqual(result["last_result"]["collection_handoff"]["study_summary"]["failed_collections"], 2)
            self.assertEqual(result["request_metrics"]["cache"]["misses"], 2)
            self.assertEqual(result["request_metrics"]["http"]["pageviews"]["attempts"], 0)
            network.assert_not_called()