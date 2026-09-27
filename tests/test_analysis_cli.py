import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.cli import main
from tools.pageviews.models import RawResponse
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


class AnalysisCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        request = make_request()
        response = RawResponse(
            url=request.url,
            fetched_at="2026-09-25T08:00:00+00:00",
            body=encode_items(
                make_item("2026071200", 100),
                make_item("2026071300", 100),
                make_item("2026071400", 120),
            ),
        )
        self.snapshot = save_snapshot(
            self.root, request, response, validate_response(response.body, request)
        )
        self.arguments = [
            "analyze",
            "--snapshot", str(self.snapshot.directory),
            "--baseline-start", "2026-07-12",
            "--baseline-end", "2026-07-13",
            "--current-start", "2026-07-14",
            "--current-end", "2026-07-14",
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(self.arguments + list(extra))
        return exit_code, json.loads(output.getvalue())

    def save_items(self, *items):
        request = self.snapshot.request
        response = replace(self.snapshot.response, body=encode_items(*items))
        return save_snapshot(
            self.root, request, response, validate_response(response.body, request)
        )

    def test_analyzes_snapshot_offline_and_leaves_files_unchanged(self):
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No network")
        ) as network:
            exit_code, result = self.invoke()

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["operation"], "analyze")
        self.assertEqual(result["comparison"]["change_percent"], 20.0)
        self.assertEqual(result["snapshot"], str(self.snapshot.directory))
        self.assertEqual(result["source"]["fetched_at_utc"], "2026-09-25T08:00:00+00:00")
        self.assertEqual(
            result["source"]["response_sha256"], self.snapshot.response.sha256
        )
        self.assertNotIn("monthly", result)
        self.assertNotIn("days", result)
        network.assert_not_called()
        after = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_monthly_details_are_available_on_request(self):
        exit_code, result = self.invoke("--monthly")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(result["monthly"]["baseline"]), 1)
        self.assertEqual(len(result["monthly"]["current"]), 1)
        self.assertEqual(result["monthly"]["baseline"][0]["month"], "2026-07")
        self.assertEqual(
            result["monthly"]["baseline"][0]["mean_daily_views_observed"], 100
        )
        self.assertTrue(result["monthly"]["current"][0]["partial_calendar_month"])

    def test_user_agent_is_accepted_but_unused(self):
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No network")
        ) as network:
            exit_code, result = self.invoke("--user-agent", "trend-visor/0.1 (test@example.com)")
        self.assertEqual(exit_code, 0, result)
        network.assert_not_called()

    def test_repeated_analysis_is_identical(self):
        first_code, first = self.invoke("--monthly")
        second_code, second = self.invoke("--monthly")
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first, second)

    def test_newer_snapshot_does_not_change_selected_snapshot(self):
        self.save_items(
            make_item("2026071200", 100),
            make_item("2026071300", 100),
            make_item("2026071400", 1),
        )
        exit_code, result = self.invoke()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["snapshot"], str(self.snapshot.directory))
        self.assertEqual(result["comparison"]["change_percent"], 20.0)

    def test_partial_data_keeps_summary_but_does_not_calculate_change(self):
        snapshot = self.save_items(
            make_item("2026071200", 100), make_item("2026071400", 120)
        )
        exit_code, result = self.invoke("--snapshot", str(snapshot.directory))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["baseline"]["coverage"]["missing_days"], 1)
        self.assertEqual(result["baseline"]["mean_daily_views_observed"], 100)
        self.assertEqual(result["comparison"]["reason"], "incomplete_coverage")
        self.assertEqual(result["comparison"]["affected_periods"], ["baseline"])
        self.assertIsNone(result["comparison"]["change_percent"])

    def test_no_observations_is_not_reported_as_a_zero_total(self):
        snapshot = self.save_items()
        exit_code, result = self.invoke("--snapshot", str(snapshot.directory))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "no_observations")
        self.assertIsNone(result["baseline"]["sum_observed_views"])
        self.assertIsNone(result["current"]["mean_daily_views_observed"])
        self.assertIsNone(result["comparison"]["change_percent"])

    def test_zero_baseline_has_a_distinct_reason(self):
        snapshot = self.save_items(
            make_item("2026071200", 0),
            make_item("2026071300", 0),
            make_item("2026071400", 120),
        )
        exit_code, result = self.invoke("--snapshot", str(snapshot.directory))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["comparison"]["reason"], "zero_baseline")
        self.assertIsNone(result["comparison"]["change_percent"])

    def test_invalid_periods_return_structured_input_errors(self):
        cases = (
            ("--baseline-start", "2026-02-30"),
            ("--baseline-end", "2026-07-11"),
            ("--current-start", "2026-07-13"),
            ("--current-end", "2026-07-15"),
            ("--baseline-start", "2026-07-11"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                exit_code, result = self.invoke(*arguments)
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_request")

    def test_invalid_arguments_return_json(self):
        exit_code, result = self.invoke("--refresh")
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_missing_snapshot_returns_error_without_downloading(self):
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No network")
        ) as network:
            exit_code, result = self.invoke("--snapshot", str(self.root / "absent"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "snapshot_error")
        network.assert_not_called()

    def test_corrupt_snapshot_is_rejected_instead_of_analyzed(self):
        for name in ("raw.json", "series.json", "metadata.json"):
            with self.subTest(name=name):
                path = self.snapshot.directory / name
                original = path.read_bytes()
                try:
                    path.write_bytes(b"corrupted")
                    exit_code, result = self.invoke()
                    self.assertEqual(exit_code, 1)
                    self.assertEqual(result["error"]["code"], "snapshot_error")
                    self.assertNotIn("comparison", result)
                finally:
                    path.write_bytes(original)

    def test_analyze_help_is_available_through_module_entry_point(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "tools.pageviews", "analyze", "--help"],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--snapshot", result.stdout)
        self.assertIn("--monthly", result.stdout)