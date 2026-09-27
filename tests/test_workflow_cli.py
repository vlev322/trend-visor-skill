import io
import hashlib
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_discovery import QUESTION, SCOPE
from tools.pageviews.discovery import begin_discovery
from tools.model_eval.workflow import main


class WorkflowCliTests(unittest.TestCase):
    @patch("tools.model_eval.config.load_config", side_effect=AssertionError("Do not read credentials"))
    def test_dry_run_is_read_only_and_works_without_optional_dependencies(self, config):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "state.json")
            arguments = ["--kind", "discovery", "--state", str(state.path), state.sha256,
                         "--output-dir", str(root / "run"), "--dry-run"]
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(arguments)
            self.assertEqual(code, 0, out.getvalue())
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "prepared_not_executed")
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["context_preflight"]["status"], "capacity_unverified")
            self.assertGreater(result["context_preflight"]["input_utf8_bytes"], 0)
            self.assertEqual(set(result["instruction_sha256"]), {"references/discovery-workflow.md"})
            self.assertEqual(result["instruction_sha256"]["references/discovery-workflow.md"],
                             hashlib.sha256((Path(__file__).resolve().parents[1] / "references/discovery-workflow.md").read_bytes()).hexdigest())
            self.assertGreater(result["instruction_utf8_bytes"]["references/discovery-workflow.md"], 0)
            operations = [
                branch["properties"]["operation"]["enum"][0]
                for branch in result["request"]["tool"]["function"]["parameters"]["oneOf"]
            ]
            self.assertNotIn("search", operations)
            self.assertFalse((root / "run").exists())
            process = subprocess.run([sys.executable, "-S", "-m", "tools.model_eval.workflow", *arguments],
                                     cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            self.assertEqual(json.loads(process.stdout)["status"], "prepared_not_executed")
        config.assert_not_called()

    @patch("tools.model_eval.config.load_config", side_effect=AssertionError("Do not read credentials before context confirmation"))
    def test_live_mode_without_checked_capacity_is_blocked_before_credentials(self, config):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, output=root / "state.json")
            arguments = ["--kind", "discovery", "--state", str(state.path), state.sha256,
                         "--output-dir", str(root / "run"), "--run-model"]
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["error"]["code"], "context_capacity_unverified")
        config.assert_not_called()

    @patch("tools.model_eval.config.load_config", side_effect=AssertionError("Invalid context values must fail before credentials"))
    def test_invalid_context_declaration_is_rejected_before_credentials(self, config):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, output=root / "state.json")
            arguments = ["--kind", "discovery", "--state", str(state.path), state.sha256,
                         "--output-dir", str(root / "run"), "--run-model",
                         "--context-window-tokens", "0", "--context-source", "provider docs"]
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["error"]["code"], "invalid_request")
        config.assert_not_called()

    @patch("tools.model_eval.config.load_config", side_effect=AssertionError("Tariff validation is offline"))
    def test_partial_tariff_is_rejected_before_credentials(self, config):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, output=root / "state.json")
            arguments = ["--kind", "discovery", "--state", str(state.path), state.sha256,
                         "--output-dir", str(root / "run"), "--dry-run",
                         "--input-rate-per-million", "0.15"]
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_request")
        config.assert_not_called()

    def test_explicit_execution_mode_and_valid_limits_are_required(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = begin_discovery(question=QUESTION, output=root / "state.json")
            base = ["--kind", "discovery", "--state", str(state.path), state.sha256, "--output-dir", str(root / "run")]
            for extra in ([], ["--dry-run", "--max-calls", "0"], ["--dry-run", "--allow-lookup"],
                          ["--dry-run", "--max-tokens", "0"]):
                out = io.StringIO()
                with self.subTest(extra=extra), redirect_stdout(out):
                    self.assertNotEqual(main(base + extra), 0)
                self.assertIn("error", json.loads(out.getvalue()))