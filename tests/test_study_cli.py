import hashlib
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
from urllib.error import HTTPError, URLError

from tests.helpers import make_request
from tests.study_helpers import TITLES, pageviews_http_response, resolution_result
from tools.pageviews.cli import main
from tools.pageviews.models import RawResponse
from tools.pageviews.resolutions import save_resolution
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


class StudyCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.saved = save_resolution(
            resolution_result(unmatched=("pl",)), self.root / "resolution.json"
        )
        self.arguments = [
            "study", "--resolution", str(self.saved.path),
            "--confirm-sha256", self.saved.sha256,
            "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
            "--current-start", "2026-07-03", "--current-end", "2026-07-04",
            "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(self.arguments + list(extra))
        return code, json.loads(output.getvalue())

    def select_languages(self, languages, unmatched=()):
        self.saved = save_resolution(
            resolution_result(languages, unmatched), self.root / "selected.json"
        )
        self.arguments.extend([
            "--resolution", str(self.saved.path), "--confirm-sha256", self.saved.sha256,
        ])

    @patch("tools.pageviews.client.urlopen")
    def test_collects_only_matches_and_reuses_snapshots_offline(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, first = self.invoke(
            "--user-agent", "study-tests/1 (offline fixture)",
            "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(first["status"], "partial")
        rows = first["results"]
        self.assertEqual([row["language"] for row in rows], ["cs", "pl"])
        self.assertEqual(rows[0]["status"], "analyzed")
        self.assertEqual(rows[0]["analysis"]["comparison"]["change_percent"], 200.0)
        self.assertFalse(rows[0]["cache_hit"])
        self.assertEqual(rows[1]["status"], "not_collected")
        self.assertEqual(rows[1]["reason"], "no_sitelink")
        self.assertNotIn("analysis", rows[1])
        opener.assert_called_once()

        before = {
            path: path.read_bytes() for path in (self.root / "cache").rglob("*")
            if path.is_file()
        }
        opener.side_effect = AssertionError("Offline study must not use HTTP")
        code, second = self.invoke("--offline", "--output", str(self.root / "second.json"))
        self.assertEqual(code, 0)
        self.assertTrue(second["results"][0]["cache_hit"])
        self.assertEqual(rows[0]["artifacts"], second["results"][0]["artifacts"])
        self.assertEqual(rows[0]["analysis"], second["results"][0]["analysis"])
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in (self.root / "cache").rglob("*") if path.is_file()},
        )
        saved = json.loads((self.root / "second.json").read_text())
        self.assertEqual(saved["results"], second["results"])
        self.assertEqual(saved["resolution"]["sha256"], self.saved.sha256)
        self.assertEqual(
            second["artifacts"]["study_sha256"],
            hashlib.sha256((self.root / "second.json").read_bytes()).hexdigest(),
        )

    @patch("tools.pageviews.client.urlopen")
    def test_comparison_keeps_language_order_and_distinguishes_levels_from_change(self, opener):
        self.select_languages(("pl", "cs", "en"), unmatched=("en",))
        opener.side_effect = [
            pageviews_http_response("pl", (100, 200, 200, 400)),
            pageviews_http_response("cs", (10, 20, 30, 60)),
        ]
        code, result = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "comparison.json"),
        )
        self.assertEqual(code, 0)
        comparison = result["comparison"]
        self.assertEqual(comparison["status"], "available")
        self.assertEqual(comparison["eligible_languages"], ["pl", "cs"])
        self.assertEqual(comparison["excluded_languages"], [{"language": "en", "reason": "no_sitelink"}])
        rows = comparison["rows"]
        self.assertEqual([row["language"] for row in rows], ["pl", "cs", "en"])
        self.assertEqual([row["change_percent"] for row in rows], [100.0, 200.0, None])
        self.assertEqual([row["current_mean_daily_views_observed"] for row in rows], [300.0, 45.0, None])
        self.assertFalse(comparison["statistical_inference_performed"])
        self.assertNotIn("winner", comparison)
        self.assertNotIn("ranking", comparison)
        for row in result["results"][:2]:
            self.assertEqual(row["analysis"]["baseline"]["window"], result["periods"]["baseline"])
            self.assertEqual(row["analysis"]["current"]["window"], result["periods"]["current"])

    @patch("tools.pageviews.client.urlopen")
    def test_missing_days_and_zero_baseline_are_excluded_without_losing_levels(self, opener):
        self.select_languages(("cs", "pl", "en"))
        opener.side_effect = [
            pageviews_http_response("cs", (10, None, 30, 60)),
            pageviews_http_response("pl", (0, 0, 10, 20)),
            pageviews_http_response("en", (20, 20, 10, 10)),
        ]
        code, result = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "partial.json"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "partial")
        comparison = result["comparison"]
        self.assertEqual(comparison["status"], "not_computed")
        self.assertEqual(comparison["reason"], "fewer_than_two_comparable_languages")
        self.assertEqual(comparison["eligible_languages"], ["en"])
        rows = comparison["rows"]
        self.assertEqual([row["change_percent"] for row in rows], [None, None, -50.0])
        self.assertEqual([row["reason"] for row in rows], ["incomplete_coverage", "zero_baseline", None])
        self.assertEqual(rows[0]["baseline_coverage"]["missing_days"], 1)
        self.assertEqual(rows[0]["baseline_mean_daily_views_observed"], 10.0)
        self.assertEqual(rows[1]["baseline_coverage"]["explicit_zero_days"], 2)
        self.assertEqual(rows[1]["baseline_mean_daily_views_observed"], 0.0)

    @patch("tools.pageviews.client.urlopen")
    def test_empty_series_is_unknown_instead_of_a_zero_series(self, opener):
        opener.return_value = pageviews_http_response("cs", (None, None, None, None))
        code, result = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "empty.json"),
        )
        self.assertEqual(code, 0)
        analysis = result["results"][0]["analysis"]
        self.assertEqual(analysis["status"], "no_observations")
        self.assertIsNone(analysis["current"]["sum_observed_views"])
        self.assertEqual(result["comparison"]["eligible_languages"], [])
        self.assertIsNone(result["comparison"]["rows"][0]["current_mean_daily_views_observed"])

    @patch("tools.pageviews.client.urlopen")
    def test_gap_outside_selected_periods_does_not_make_them_incomplete(self, opener):
        self.select_languages(("cs",))
        opener.return_value = pageviews_http_response("cs", (10, None, 30, 60))
        code, result = self.invoke(
            "--baseline-end", "2026-07-01", "--user-agent", "study-tests/1",
            "--output", str(self.root / "gap.json"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "complete")
        row = result["results"][0]
        self.assertEqual(row["coverage"]["missing_days"], 1)
        self.assertEqual(row["analysis"]["status"], "complete")
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 350.0)
        self.assertEqual(result["comparison"]["status"], "not_computed")

    @patch("tools.pageviews.client.urlopen")
    def test_offline_cache_miss_still_saves_all_language_outcomes(self, opener):
        code, result = self.invoke("--offline", "--output", str(self.root / "offline.json"))
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual([row["reason"] for row in result["results"]], ["cache_miss", "no_sitelink"])
        self.assertTrue((self.root / "offline.json").is_file())
        self.assertFalse((self.root / "cache").exists())
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_related_periods_can_reuse_same_outer_window_but_as_of_changes_cache_key(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        opener.side_effect = AssertionError("Related offline requests must not use HTTP")
        code, related = self.invoke(
            "--offline", "--baseline-end", "2026-07-01", "--monthly",
            "--output", str(self.root / "related.json"),
        )
        self.assertEqual(code, 0)
        row = related["results"][0]
        self.assertEqual(row["snapshot"], first["results"][0]["snapshot"])
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 350.0)
        self.assertTrue(row["analysis"]["monthly"]["baseline"][0]["partial_calendar_month"])
        code, changed = self.invoke(
            "--offline", "--as-of", "2026-09-26", "--output", str(self.root / "changed.json"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(changed["results"][0]["reason"], "cache_miss")
        opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_refresh_keeps_old_snapshot_and_records_new_observation(self, opener):
        opener.side_effect = [
            pageviews_http_response("cs", (10, 20, 30, 60)),
            pageviews_http_response("cs", (10, 20, 40, 80)),
        ]
        code, first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        old = Path(first["results"][0]["artifacts"]["raw"])
        before = old.read_bytes()
        code, refreshed = self.invoke(
            "--refresh", "--user-agent", "study-tests/1",
            "--output", str(self.root / "refreshed.json"),
        )
        self.assertEqual(code, 0)
        row = refreshed["results"][0]
        self.assertFalse(row["cache_hit"])
        self.assertNotEqual(row["snapshot"], first["results"][0]["snapshot"])
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 300.0)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(opener.call_count, 2)

    @patch("tools.pageviews.client.urlopen")
    def test_corrupt_cache_is_not_silently_refetched(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        Path(first["results"][0]["artifacts"]["raw"]).write_bytes(b"damaged fixture")
        code, second = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "second.json"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(second["results"][0]["reason"], "cache_error")
        opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_service_limits_stop_new_http_but_allow_later_cached_languages(self, opener):
        self.select_languages(("cs", "pl", "en"))
        request = make_request(
            project="pl.wikipedia.org", article=TITLES["pl"], start="2026-07-01", end="2026-07-04",
        )
        body = pageviews_http_response("pl", (10, 20, 30, 60)).getvalue()
        response = RawResponse(request.url, "2026-09-25T08:00:00+00:00", body)
        save_snapshot(self.root / "cache", request, response, validate_response(body, request))
        for status in (403, 429, 503):
            with self.subTest(status=status):
                opener.reset_mock()
                opener.side_effect = HTTPError(
                    "https://wikimedia.org/fixture", status, "Test limit", {"Retry-After": "60"}, None,
                )
                code, result = self.invoke(
                    "--user-agent", "study-tests/1", "--output", str(self.root / f"limit-{status}.json"),
                )
                self.assertEqual(code, 1)
                first, cached, stopped = result["results"]
                self.assertEqual(first["error"]["details"]["http_status"], status)
                self.assertEqual(first["error"]["details"]["retry_after"], "60")
                self.assertTrue(cached["cache_hit"])
                self.assertEqual(stopped["reason"], "collection_stopped")
                self.assertEqual(result["summary"]["failed_collections"], 2)
                opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_individual_unavailable_or_failed_response_does_not_drop_other_languages(self, opener):
        self.select_languages(("cs", "pl"))
        for name, failure, reason in (
            ("not-found", HTTPError("https://wikimedia.org/fixture", 404, "Fixture", {}, None), "data_unavailable"),
            ("network", URLError("Fixture failure"), "network_error"),
            ("invalid", pageviews_http_response("cs", (10, "bad", 30, 60)), "invalid_response"),
        ):
            with self.subTest(name=name):
                opener.reset_mock()
                opener.side_effect = [failure, pageviews_http_response("pl", (10, 20, 30, 60))]
                code, result = self.invoke(
                    "--user-agent", "study-tests/1", "--cache-dir", str(self.root / name),
                    "--output", str(self.root / f"{name}.json"),
                )
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["results"][0]["reason"], reason)
                self.assertNotIn("analysis", result["results"][0])
                self.assertEqual(result["results"][1]["status"], "analyzed")
                self.assertEqual(opener.call_count, 2)

    @patch("tools.pageviews.client.urlopen")
    def test_validation_errors_happen_before_network_or_output_writes(self, opener):
        cases = (
            ("--current-end", "2026-09-18"),
            ("--baseline-end", "2026-07-03"),
            ("--baseline-end", "2026-06-30"),
            ("--baseline-start", "2015-06-30"),
            ("--as-of", "2026-02-30"),
            ("--lag-days", "-1"),
            ("--timeout", "nan"),
            ("--timeout", "0"),
            ("--offline", "--refresh"),
            ("--user-agent", ""),
        )
        for index, arguments in enumerate(cases):
            with self.subTest(arguments=arguments):
                destination = self.root / f"invalid-{index}.json"
                code, result = self.invoke(
                    "--user-agent", "study-tests/1", "--output", str(destination), *arguments,
                )
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertFalse(destination.exists())
        code, result = self.invoke("--output", str(self.root / "no-user-agent.json"))
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "invalid_request")
        opener.assert_not_called()
        self.assertFalse((self.root / "cache").exists())

    @patch("tools.pageviews.client.urlopen")
    def test_confirmation_is_mandatory_and_changed_file_never_fetches(self, opener):
        output = self.root / "blocked.json"
        code, result = self.invoke(
            "--confirm-sha256", "0" * 64, "--user-agent", "study-tests/1", "--output", str(output),
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "confirmation_mismatch")
        original = self.saved.path.read_bytes()
        self.saved.path.write_bytes(original + b"\n")
        code, result = self.invoke("--offline", "--output", str(output))
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "confirmation_mismatch")
        index = self.arguments.index("--confirm-sha256")
        del self.arguments[index:index + 2]
        code, result = self.invoke("--offline", "--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertFalse(output.exists())
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_unresolved_topic_is_reported_without_pageview_requests(self, opener):
        self.select_languages(("cs", "pl"), unmatched=("cs", "pl"))
        code, result = self.invoke("--offline", "--output", str(self.root / "unresolved.json"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["comparison"]["eligible_languages"], [])
        self.assertTrue(all(row["reason"] == "no_sitelink" for row in result["results"]))
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_existing_output_blocks_collection_without_overwriting(self, opener):
        destination = self.root / "existing.json"
        destination.write_bytes(b"previous research")
        code, result = self.invoke("--user-agent", "study-tests/1", "--output", str(destination))
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "output_exists")
        self.assertEqual(destination.read_bytes(), b"previous research")
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_default_study_artifact_is_unique(self, opener):
        with patch("tools.pageviews.study_cli.ASSETS", self.root / "assets"):
            first_code, first = self.invoke("--offline")
            second_code, second = self.invoke("--offline")
        self.assertEqual((first_code, second_code), (1, 1))
        self.assertNotEqual(first["artifacts"]["study"], second["artifacts"]["study"])
        for result in (first, second):
            artifact = Path(result["artifacts"]["study"])
            self.assertEqual(artifact.parent, self.root / "assets" / "studies")
            self.assertTrue(artifact.is_file())
        opener.assert_not_called()

    def test_help_works_without_optional_dependencies(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "tools.pageviews", "study", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--confirm-sha256", result.stdout)
        self.assertIn("--offline", result.stdout)