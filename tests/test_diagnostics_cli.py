import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.cli import main
from tools.pageviews.models import RawResponse
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


class DiagnosticsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        request = make_request(start="2026-01-01", end="2026-01-10")
        values = [100] * 5 + [50, 50, None, 50, 100]
        items = []
        for index, value in enumerate(values):
            if value is not None:
                day = date(2026, 1, 1) + timedelta(days=index)
                items.append(make_item(day.strftime("%Y%m%d00"), value))
        response = RawResponse(
            url=request.url,
            fetched_at="2026-09-25T08:00:00+00:00",
            body=encode_items(*items),
        )
        self.snapshot = save_snapshot(
            self.root, request, response, validate_response(response.body, request)
        )
        self.arguments = [
            "analyze",
            "--snapshot", str(self.snapshot.directory),
            "--baseline-start", "2026-01-01",
            "--baseline-end", "2026-01-05",
            "--current-start", "2026-01-06",
            "--current-end", "2026-01-10",
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(self.arguments + list(extra))
        return exit_code, json.loads(output.getvalue())

    def test_diagnostics_add_scenarios_without_changing_analysis_or_files(self):
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No network")
        ) as network:
            plain_code, plain = self.invoke()
            checked_code, checked = self.invoke(
                "--diagnostics", "--top-days", "1", "--trim-days", "1",
                "--missing-daily-upper-bound", "150",
            )

        self.assertEqual((plain_code, checked_code), (0, 0))
        diagnostics = checked.pop("diagnostics")
        self.assertEqual(checked, plain)
        self.assertEqual(diagnostics["parameters"], {
            "top_days": 1, "trim_days": 1, "missing_daily_upper_bound": 150,
        })
        missing = diagnostics["missing_values"]
        self.assertEqual(missing["break_even"]["required_missing_views_total"], 250)
        self.assertEqual(missing["conditional_bounds"]["lower_change_percent"], -50)
        self.assertEqual(missing["conditional_bounds"]["upper_change_percent"], -20)
        self.assertIsNone(checked["comparison"]["change_percent"])
        self.assertEqual(diagnostics["largest_days"]["requested_top_days_per_period"], 1)
        self.assertEqual(diagnostics["window_edges"]["trim_days_per_edge"], 1)
        network.assert_not_called()
        after = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_defaults_are_explicit_without_inventing_a_missing_value_cap(self):
        exit_code, result = self.invoke("--diagnostics")
        self.assertEqual(exit_code, 0)
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["parameters"], {
            "top_days": 3, "trim_days": 7, "missing_daily_upper_bound": None,
        })
        self.assertEqual(
            diagnostics["kind"], "sensitivity_checks_not_statistical_inference"
        )
        bounds = diagnostics["missing_values"]["conditional_bounds"]
        self.assertEqual(bounds["status"], "not_computed")
        self.assertEqual(bounds["reason"], "missing_upper_bound_required")
        self.assertEqual(diagnostics["window_edges"]["reason"], "period_too_short")

    def test_repeated_diagnostics_are_identical(self):
        first_code, first = self.invoke("--diagnostics")
        second_code, second = self.invoke("--diagnostics")
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first, second)

    def test_monthly_details_are_unchanged_when_diagnostics_are_requested(self):
        plain_code, plain = self.invoke("--monthly")
        checked_code, checked = self.invoke("--monthly", "--diagnostics")
        self.assertEqual((plain_code, checked_code), (0, 0))
        checked.pop("diagnostics")
        self.assertEqual(checked, plain)

    def test_changed_cap_only_changes_the_conditional_scenario(self):
        first_code, first = self.invoke(
            "--diagnostics", "--missing-daily-upper-bound", "150"
        )
        second_code, second = self.invoke(
            "--diagnostics", "--missing-daily-upper-bound", "300"
        )
        self.assertEqual((first_code, second_code), (0, 0))
        first_diagnostics = first.pop("diagnostics")
        second_diagnostics = second.pop("diagnostics")
        self.assertEqual(first, second)
        first_bounds = first_diagnostics["missing_values"]["conditional_bounds"]
        second_bounds = second_diagnostics["missing_values"]["conditional_bounds"]
        self.assertEqual(first_bounds["upper_change_percent"], -20)
        self.assertEqual(second_bounds["upper_change_percent"], 10)
        self.assertEqual(
            first_diagnostics["largest_days"], second_diagnostics["largest_days"]
        )
        self.assertEqual(
            first_diagnostics["window_edges"], second_diagnostics["window_edges"]
        )

    def test_scenario_parameters_cannot_be_silently_ignored(self):
        for option in ("--top-days", "--trim-days", "--missing-daily-upper-bound"):
            with self.subTest(option=option):
                exit_code, result = self.invoke(option, "1")
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertIn("--diagnostics", result["error"]["message"])

    def test_invalid_scenario_values_return_structured_errors(self):
        for option, value in (
            ("--top-days", "0"), ("--top-days", "-1"),
            ("--trim-days", "0"), ("--trim-days", "-1"),
            ("--missing-daily-upper-bound", "-1"),
        ):
            with self.subTest(option=option, value=value):
                exit_code, result = self.invoke("--diagnostics", option, value)
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["error"]["code"], "invalid_request")
        exit_code, result = self.invoke("--diagnostics", "--top-days", "1.5")
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_corrupt_snapshot_is_not_used_for_diagnostics(self):
        (self.snapshot.directory / "raw.json").write_bytes(b"corrupted")
        exit_code, result = self.invoke("--diagnostics")
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "snapshot_error")
        self.assertNotIn("diagnostics", result)

    def test_diagnostics_run_without_third_party_packages(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-S", "-m", "tools.pageviews", *self.arguments,
             "--diagnostics"],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        diagnostics = json.loads(result.stdout)["diagnostics"]
        missing = diagnostics["missing_values"]
        self.assertEqual(missing["break_even"]["required_missing_views_total"], 250)