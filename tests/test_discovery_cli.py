import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_discovery import QUESTION, SCOPE, lookup_responses, views_response
from tools.pageviews.artifacts import save_json_artifact
from tools.pageviews.cli import main


class DiscoveryCliTests(unittest.TestCase):
    def invoke(self, *arguments):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(list(arguments))
        return code, json.loads(stream.getvalue())

    @patch("tools.pageviews.client.urlopen")
    @patch("tools.pageviews.topics.fetch_action")
    def test_cli_connects_question_discovery_approval_study_and_resumed_report(self, lookup, network):
        lookup.side_effect = lookup_responses()
        network.side_effect = [views_response("cs", "Městské_zahradničení", (10, 20, 30, 60)),
                               views_response("pl", "Ogrodnictwo_miejskie", (100, 200, 200, 400))]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scope = save_json_artifact(SCOPE, root / "scope.json")
            code, result = self.invoke("discovery", "begin", "--question", QUESTION, "--output", str(root / "begin.json"))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["next_action"], "clarify_scope")
            for action, extra in (
                ("revise", ("--question", QUESTION, "--scope", str(scope.path), scope.sha256)),
                ("search", ("--user-agent", "discovery-tests/1")),
                ("resolve", ("--entity", "Q9001", "--user-agent", "discovery-tests/1")),
            ):
                code, result = self.invoke("discovery", action, "--state", result["state"]["path"], result["state"]["sha256"],
                                           *extra, "--output", str(root / f"{action}.json"))
                self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "awaiting_confirmation")
            code, result = self.invoke("discovery", "approve", "--state", result["state"]["path"], result["state"]["sha256"],
                                       "--confirm-sha256", result["state"]["sha256"], "--user-reply", "Тестове погодження.",
                                       "--output", str(root / "approved.json"))
            self.assertEqual(code, 0, result)
            code, result = self.invoke("discovery", "collect", "--state", result["state"]["path"], result["state"]["sha256"],
                                       "--online", "--user-agent", "discovery-tests/1", "--cache-dir", str(root / "cache"),
                                       "--output", str(root / "collected.json"))
            self.assertEqual(code, 0, result)
            code, report = self.invoke("report", "--research-state", result["research"]["path"], result["research"]["sha256"],
                                       "--markdown", str(root / "report.md"))
            self.assertEqual(code, 0, report)
            self.assertEqual(report["question"], QUESTION)
            self.assertTrue((root / "report.md").exists())

    @patch("tools.pageviews.topics.fetch_action")
    def test_cli_failed_lookup_returns_saved_state_and_nonzero_exit(self, lookup):
        from tools.pageviews.errors import PageviewsError

        lookup.side_effect = PageviewsError("api_busy", "Fixture lag")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scope = save_json_artifact(SCOPE, root / "scope.json")
            _, initial = self.invoke("discovery", "begin", "--question", QUESTION, "--scope", str(scope.path), scope.sha256,
                                      "--output", str(root / "begin.json"))
            code, result = self.invoke("discovery", "search", "--state", initial["state"]["path"], initial["state"]["sha256"],
                                       "--user-agent", "test/1", "--output", str(root / "failed.json"))
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["error"]["code"], "api_busy")
            self.assertTrue((root / "failed.json").exists())
            lookup.assert_called_once()

    def test_help_and_reload_in_new_process_work_without_optional_dependencies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, initial = self.invoke("discovery", "begin", "--question", QUESTION, "--output", str(root / "initial.json"))
            for args in (["--help"], ["collect", "--help"], ["show", "--state", initial["state"]["path"], initial["state"]["sha256"]]):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, "-S", "-m", "tools.pageviews", "discovery", *args],
                                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    if args[0] == "show":
                        self.assertEqual(json.loads(result.stdout)["question"], QUESTION)

    def test_missing_inputs_and_changed_scope_file_are_json_errors(self):
        code, result = self.invoke("discovery", "collect")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scope = save_json_artifact(SCOPE, root / "scope.json")
            scope.path.write_bytes(scope.path.read_bytes() + b"\n")
            code, result = self.invoke("discovery", "begin", "--question", QUESTION, "--scope", str(scope.path), scope.sha256,
                                       "--output", str(root / "blocked.json"))
            self.assertEqual(code, 2)
            self.assertFalse((root / "blocked.json").exists())