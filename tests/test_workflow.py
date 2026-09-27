import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_discovery import QUESTION, SCOPE, lookup_responses
from tools.pageviews.discovery import begin_discovery
from tools.pageviews.errors import PageviewsError
from tools.model_eval.workflow_tools import WorkflowAdapter
from tools.model_eval.workflow_runner import run_workflow


def model_action(operation, **fields):
    return {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-fixture", "type": "function", "function": {
            "name": "wikipedia_workflow", "arguments": json.dumps({"operation": operation, **fields})}}
    ]}}


class WorkflowAdapterTests(unittest.TestCase):
    @patch("tools.pageviews.topics.fetch_action")
    def test_model_selects_actions_but_cannot_approve_or_invent_paths(self, lookup):
        lookup.side_effect = lookup_responses()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", state, root / "run", allow_lookup=True, user_agent="workflow-tests/1")
            self.assertIn("search", adapter.allowed_operations())
            self.assertNotIn("approve", adapter.allowed_operations())
            for action in ({"operation": "approve"}, {"operation": "search", "output": ".env"}):
                with self.assertRaises(PageviewsError):
                    adapter.execute(action)
            self.assertEqual(lookup.call_count, 0)
            adapter.execute({"operation": "search"})
            result = adapter.execute({"operation": "resolve", "entity": "Q9001"})
            self.assertEqual(result["status"], "awaiting_confirmation")
            self.assertEqual(adapter.pause_status(), "awaiting_confirmation")
            self.assertEqual(adapter.allowed_operations(), [])
            with self.assertRaises(PageviewsError):
                adapter.execute({"operation": "collect"})
            self.assertEqual(lookup.call_count, 5)

    @patch("tools.pageviews.topics.fetch_action")
    def test_model_loop_uses_current_state_and_stops_before_human_approval(self, lookup):
        lookup.side_effect = lookup_responses()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            adapter = WorkflowAdapter("discovery", state, root / "run", allow_lookup=True, user_agent="workflow-tests/1")
            responses = iter([model_action("search"), model_action("resolve", entity="Q9001")])
            requests = []

            def complete(messages, tool):
                requests.append((messages, tool))
                operations = [
                    branch["properties"]["operation"]["enum"][0]
                    for branch in tool["function"]["parameters"]["oneOf"]
                ]
                self.assertNotIn("approve", operations)
                self.assertEqual(len(messages), 2)
                return next(responses)

            report = run_workflow(adapter, complete, {"SKILL.md": "Fixture skill"})
            self.assertEqual(report["status"], "awaiting_confirmation")
            self.assertEqual(report["model_calls"], 2)
            self.assertEqual(len(requests), 2)
            self.assertEqual(report["active_state"]["sha256"], adapter.reference.sha256)
            self.assertTrue((root / "run" / "workflow.json").is_file())
            self.assertEqual(len(report["events"]), 2)
            self.assertTrue(all(Path(event["request"]["path"]).is_file() for event in report["events"]))
            self.assertEqual(lookup.call_count, 5)