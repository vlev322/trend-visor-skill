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

from tests.report_helpers import saved_study
from tools.pageviews.cli import main
from tools.pageviews.reports import build_report, evidence_page


class ReportCliTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_cli_saves_full_text_but_returns_only_an_evidence_page(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            output, markdown = root / "report.json", root / "report.md"
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main([
                    "report", "--study", str(saved.path),
                    "--question", "Порівняй ці статті", "--criterion", "Покриття даних",
                    "--limit", "1", "--output", str(output), "--markdown", str(markdown),
                ])
            self.assertEqual(code, 0, stream.getvalue())
            result = json.loads(stream.getvalue())
            full = json.loads(output.read_text())
            self.assertEqual(markdown.read_text(), full["markdown"])
            self.assertEqual(result["total_rows"], 3)
            self.assertEqual(len(result["evidence"]), 1)
            self.assertNotIn("markdown", result)
            self.assertEqual(result["criteria"], ["Покриття даних"])
            self.assertEqual(result["prioritization"]["status"], "criteria_require_review")
            self.assertEqual(result["artifacts"]["markdown"], str(markdown.resolve()))
        network.assert_not_called()

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_user_agent_is_accepted_but_unused(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main([
                    "report", "--study", str(saved.path), "--question", "Питання",
                    "--user-agent", "trend-visor/0.1 (test@example.com)",
                ])
            self.assertEqual(code, 0, stream.getvalue())
        network.assert_not_called()

    def test_paging_without_outputs_is_read_only_and_existing_output_is_protected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            arguments = ["report", "--study", str(saved.path), "--question", "Порівняй"]
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main([*arguments, "--offset", "2", "--limit", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stream.getvalue())["evidence"][0]["language"], "en")
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            existing = root / "existing.md"
            existing.write_text("Keep me")
            with redirect_stdout(io.StringIO()):
                code = main([*arguments, "--output", str(root / "new.json"), "--markdown", str(existing)])
            self.assertEqual(code, 1)
            self.assertEqual(existing.read_text(), "Keep me")
            self.assertFalse((root / "new.json").exists())

    def test_errors_never_publish_partial_reports_from_bad_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            arguments = ["report", "--study", str(root / "missing.json"), "--question", "Порівняй"]
            for extra in ([], ["--offset", "99"], ["--question", ""], ["--limit", "11"]):
                with self.subTest(extra=extra), redirect_stdout(io.StringIO()):
                    base = arguments if extra == [] else [
                        "report", "--study", str(saved.path), "--question", "Порівняй",
                    ]
                    code = main([*base, "--output", str(root / "new.json"), "--markdown", str(root / "new.md"), *extra])
                    self.assertNotEqual(code, 0)
                    self.assertFalse((root / "new.json").exists())
                    self.assertFalse((root / "new.md").exists())

    def test_entire_printed_json_including_artifact_paths_is_bounded_before_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй")
            page_bytes = len(json.dumps(evidence_page(report), ensure_ascii=False, indent=2).encode()) + 1
            output = root / "new.json"
            stream = io.StringIO()
            with patch("tools.pageviews.reports.MAX_EVIDENCE_BYTES", page_bytes), redirect_stdout(stream):
                code = main(["report", "--study", str(saved.path),
                             "--question", "Порівняй", "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stream.getvalue())["error"]["code"], "evidence_too_large")
            self.assertFalse(output.exists())

    def test_help_and_report_work_without_optional_dependencies(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            for args in (["--help"], ["--study", str(saved.path), "--question", "Порівняй"]):
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, "-S", "-m", "tools.pageviews", "report", *args],
                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    self.assertTrue("--study" in result.stdout if args == ["--help"] else json.loads(result.stdout)["total_rows"] == 3)
