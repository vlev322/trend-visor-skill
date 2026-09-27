import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.report_helpers import saved_study
from tests.test_report_criteria import QUESTION, saved_rules
from tools.pageviews.cli import main


class ResearchCliTests(unittest.TestCase):
    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(args))
        return code, json.loads(output.getvalue())

    def test_cli_pause_confirm_and_resume_with_only_a_state_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            code, initial = self.invoke("research", "start", "--study", str(study.path), study.sha256,
                                        "--question", QUESTION, "--output", str(root / "initial.json"))
            self.assertEqual(code, 0, initial)
            code, proposed = self.invoke("research", "propose", "--state", initial["state"]["path"], initial["state"]["sha256"],
                                         "--rules", str(rules.path), rules.sha256, "--output", str(root / "pending.json"))
            self.assertEqual(code, 0, proposed)
            self.assertEqual(proposed["next_action"], "ask_user_to_confirm_exact_state")
            code, blocked = self.invoke("report", "--research-state", proposed["state"]["path"], proposed["state"]["sha256"],
                                        "--output", str(root / "blocked.json"))
            self.assertEqual(code, 1)
            self.assertEqual(blocked["error"]["code"], "confirmation_required")
            self.assertFalse((root / "blocked.json").exists())
            code, approved = self.invoke("research", "approve", "--state", proposed["state"]["path"], proposed["state"]["sha256"],
                                         "--confirm-sha256", proposed["state"]["sha256"], "--user-reply", "Так, підтверджую.",
                                         "--output", str(root / "approved.json"))
            self.assertEqual(code, 0, approved)
            code, result = self.invoke("report", "--research-state", approved["state"]["path"], approved["state"]["sha256"],
                                       "--markdown", str(root / "report.md"))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["question"], QUESTION)
            self.assertEqual(result["research_state"], approved["state"])
            self.assertEqual(result["prioritization"]["counts"]["matches"], 1)
            self.assertTrue((root / "report.md").is_file())

    def test_saved_inputs_cannot_be_overridden_and_show_is_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            _, initial = self.invoke("research", "start", "--study", str(study.path), study.sha256,
                                     "--question", QUESTION, "--output", str(root / "initial.json"))
            ref = (initial["state"]["path"], initial["state"]["sha256"])
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            code, shown = self.invoke("research", "show", "--state", *ref)
            self.assertEqual(code, 0)
            self.assertEqual(shown["question"], QUESTION)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            for extra in (("--question", "override"), ("--criterion", "hidden"), ("--study", str(study.path))):
                with self.subTest(extra=extra):
                    code, result = self.invoke("report", "--research-state", *ref, *extra, "--output", str(root / "bad.json"))
                    self.assertEqual(code, 2)
                    self.assertEqual(result["error"]["code"], "invalid_request")
                    self.assertFalse((root / "bad.json").exists())
            code, revised = self.invoke("research", "revise", "--state", *ref, "--question", "Нове питання",
                                        "--output", str(root / "revised.json"))
            self.assertEqual(code, 0)
            self.assertEqual(revised["question"], "Нове питання")

    def test_new_process_resumes_without_extras_or_chat_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            _, initial = self.invoke("research", "start", "--study", str(study.path), study.sha256,
                                     "--question", QUESTION, "--output", str(root / "initial.json"))
            commands = (["research", "--help"], ["research", "approve", "--help"],
                        ["report", "--research-state", initial["state"]["path"], initial["state"]["sha256"], "--offset", "2"])
            for args in commands:
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, "-S", "-m", "tools.pageviews", *args],
                                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if args[0] == "report":
                        self.assertEqual(json.loads(result.stdout)["evidence"][0]["language"], "en")

    def test_missing_arguments_are_json_errors(self):
        for args in (("research",), ("research", "approve"), ("report",)):
            with self.subTest(args=args):
                code, result = self.invoke(*args)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "invalid_arguments")