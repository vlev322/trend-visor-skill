import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tests.methodology_helpers import known_trend_series
from tests.study_helpers import TITLES, resolution_result
from tools.pageviews.cli import main
from tools.pageviews.models import RawResponse
from tools.pageviews.resolutions import save_resolution
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


class MethodologyCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        request = make_request(start="2024-01-01", end="2025-12-31")
        body = encode_items(*(
            make_item(day.day.strftime("%Y%m%d00"), day.views) for day in known_trend_series().days
        ))
        response = RawResponse(request.url, "2026-09-25T08:00:00+00:00", body)
        self.snapshot = save_snapshot(self.root, request, response, validate_response(body, request))
        self.arguments = [
            "analyze", "--snapshot", str(self.snapshot.directory),
            "--baseline-start", "2024-01-01", "--baseline-end", "2024-12-31",
            "--current-start", "2025-01-01", "--current-end", "2025-12-31",
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(self.arguments + list(extra))
        return code, json.loads(output.getvalue())

    def test_methodology_is_opt_in_and_preserves_original_analysis_and_snapshots(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network")) as network:
            plain_code, plain = self.invoke("--monthly", "--diagnostics")
            code, result = self.invoke("--monthly", "--diagnostics", "--methodology")
        self.assertEqual((plain_code, code), (0, 0))
        block = result.pop("methodology")
        self.assertEqual(result, plain)
        self.assertEqual(block["calendar_comparison"]["summary"]["computed_pairs"], 12)
        self.assertEqual(block["trend_model"]["status"], "not_requested")
        self.assertFalse(block["statistical_inference_performed"])
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        network.assert_not_called()

    @unittest.skipUnless(find_spec("statsmodels") is not None, "Optional statistics extra is not installed")
    def test_explicit_model_and_lag_add_conditional_interval_not_a_growth_percentage(self):
        code, result = self.invoke("--methodology", "--trend-model", "linear-calendar-hac", "--hac-lags", "7")
        self.assertEqual(code, 0)
        model = result["methodology"]["trend_model"]
        self.assertAlmostEqual(model["slope_daily_views_per_year"], 365.25, places=8)
        self.assertEqual(model["confidence_interval"]["status"], "computed")
        self.assertTrue(result["methodology"]["statistical_inference_performed"])
        self.assertEqual(result["methodology"]["parameters"], {"trend_model": "linear-calendar-hac", "hac_lags": 7})
        _, plain = self.invoke()
        result.pop("methodology")
        self.assertEqual(result, plain)

    def test_method_parameters_cannot_be_ignored_or_chosen_implicitly(self):
        for arguments in (
            ("--hac-lags", "7"),
            ("--trend-model", "linear-calendar-hac"),
            ("--methodology", "--hac-lags", "7"),
            ("--methodology", "--trend-model", "linear-calendar-hac"),
            ("--methodology", "--trend-model", "linear-calendar-hac", "--hac-lags", "-1"),
            ("--methodology", "--trend-model", "linear-calendar-hac", "--hac-lags", "731"),
            ("--methodology", "--trend-model", "other"),
        ):
            with self.subTest(arguments=arguments):
                code, result = self.invoke(*arguments)
                self.assertEqual(code, 2)
                self.assertIn(result["error"]["code"], {"invalid_arguments", "invalid_request"})

    def test_calendar_methodology_and_help_work_without_optional_packages(self):
        root = Path(__file__).resolve().parents[1]
        for arguments in (self.arguments + ["--methodology"], ["analyze", "--help"], ["study", "--help"]):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, "-S", "-m", "tools.pageviews", *arguments], cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("methodology", completed.stdout)

    def test_explicit_model_without_statistics_extra_returns_actionable_error(self):
        completed = subprocess.run(
            [sys.executable, "-S", "-m", "tools.pageviews", *self.arguments, "--methodology",
             "--trend-model", "linear-calendar-hac", "--hac-lags", "7"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        error = json.loads(completed.stdout)["error"]
        self.assertEqual(error["code"], "missing_dependency")
        self.assertIn("--extra statistics", error["message"])


class MethodologyStudyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        resolution = save_resolution(
            resolution_result(("cs", "pl", "en"), unmatched=("en",)),
            self.root / "resolution.json",
        )
        for language in ("cs", "pl"):
            request = make_request(
                project=f"{language}.wikipedia.org", article=TITLES[language],
                start="2024-01-01", end="2025-12-31",
            )
            body = encode_items(*(
                make_item(
                    day.day.strftime("%Y%m%d00"), day.views,
                    project=request.project, article=request.article,
                )
                for day in known_trend_series().days
                if language == "pl" or day.day.isoformat() != "2025-06-01"
            ))
            response = RawResponse(request.url, "2026-09-25T08:00:00+00:00", body)
            save_snapshot(self.cache, request, response, validate_response(body, request))
        self.arguments = [
            "study", "--resolution", str(resolution.path), "--confirm-sha256", resolution.sha256,
            "--baseline-start", "2024-01-01", "--baseline-end", "2024-12-31",
            "--current-start", "2025-01-01", "--current-end", "2025-12-31",
            "--as-of", "2026-09-25", "--cache-dir", str(self.cache), "--offline",
        ]

    def invoke(self, name, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(self.arguments + ["--output", str(self.root / name), *extra])
        return code, json.loads(output.getvalue())

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Offline study"))
    def test_calendar_evidence_preserves_descriptive_table_and_unresolved_languages(self, network):
        before = {path: path.read_bytes() for path in self.cache.rglob("*") if path.is_file()}
        plain_code, plain = self.invoke("plain.json")
        code, result = self.invoke("methodology.json", "--methodology")
        self.assertEqual((plain_code, code), (0, 0))
        self.assertEqual(result["comparison"], plain["comparison"])
        self.assertEqual([row["language"] for row in result["results"]], ["cs", "pl", "en"])
        self.assertEqual(result["results"][2], plain["results"][2])
        self.assertEqual(result["methodology"]["models_with_intervals"], 0)
        calendar = result["results"][0]["analysis"]["methodology"]["calendar_comparison"]
        self.assertEqual(calendar["summary"]["computed_pairs"], 11)
        self.assertEqual(calendar["summary"]["excluded_pairs"], 1)
        self.assertEqual(before, {path: path.read_bytes() for path in self.cache.rglob("*") if path.is_file()})
        network.assert_not_called()

    @unittest.skipUnless(find_spec("statsmodels") is not None, "Optional statistics extra is not installed")
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Offline study"))
    def test_individual_slope_interval_does_not_become_a_between_language_test(self, network):
        code, result = self.invoke(
            "model.json", "--methodology", "--trend-model", "linear-calendar-hac", "--hac-lags", "7",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["methodology"]["models_fitted"], 1)
        self.assertEqual(result["methodology"]["models_with_intervals"], 1)
        self.assertEqual(
            result["methodology"]["inference_scope"], "individual_articles_not_between_language_differences",
        )
        first, second, unresolved = result["results"]
        self.assertEqual(first["analysis"]["methodology"]["trend_model"]["reason"], "incomplete_coverage")
        self.assertAlmostEqual(second["analysis"]["methodology"]["trend_model"]["slope_daily_views_per_year"], 365.25)
        self.assertEqual(unresolved["reason"], "no_sitelink")
        self.assertFalse(result["comparison"]["statistical_inference_performed"])
        self.assertNotIn("winner", result["comparison"])
        network.assert_not_called()