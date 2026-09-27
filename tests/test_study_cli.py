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
from tools.pageviews.artifacts import save_json_artifact
from tools.pageviews.cli import main
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import RawResponse
from tools.pageviews.reports import build_report
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
            "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
            "--current-start", "2026-07-03", "--current-end", "2026-07-04",
            "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(self.arguments + list(extra))
        return code, json.loads(output.getvalue())

    def full(self, result):
        return json.loads(Path(result["artifacts"]["study"]).read_text())

    def select_languages(self, languages, unmatched=()):
        self.saved = save_resolution(
            resolution_result(languages, unmatched), self.root / "selected.json"
        )
        index = self.arguments.index("--resolution")
        self.arguments[index:index + 2] = ["--resolution", str(self.saved.path)]

    @patch("tools.pageviews.client.urlopen")
    def test_collects_only_matches_and_reuses_snapshots_offline(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, compact_first = self.invoke(
            "--user-agent", "study-tests/1 (offline fixture)",
            "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        first = self.full(compact_first)
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
        code, compact_second = self.invoke("--offline", "--output", str(self.root / "second.json"))
        self.assertEqual(code, 0)
        second = self.full(compact_second)
        self.assertTrue(second["results"][0]["cache_hit"])
        self.assertEqual(rows[0]["artifacts"], second["results"][0]["artifacts"])
        self.assertEqual(rows[0]["analysis"], second["results"][0]["analysis"])
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in (self.root / "cache").rglob("*") if path.is_file()},
        )
        saved = json.loads((self.root / "second.json").read_text())
        self.assertEqual(saved["results"], second["results"])
        self.assertEqual(saved["resolutions"][0]["sha256"], self.saved.sha256)
        self.assertEqual(
            compact_second["artifacts"]["study"],
            str((self.root / "second.json").resolve()),
        )

    @patch("tools.pageviews.client.urlopen")
    def test_comparison_keeps_language_order_and_distinguishes_levels_from_change(self, opener):
        self.select_languages(("pl", "cs", "en"), unmatched=("en",))
        opener.side_effect = [
            pageviews_http_response("pl", (100, 200, 200, 400)),
            pageviews_http_response("cs", (10, 20, 30, 60)),
        ]
        code, compact = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "comparison.json"),
        )
        self.assertEqual(code, 0)
        result = self.full(compact)
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
    def test_days_without_an_api_row_count_as_zero_but_zero_baseline_still_blocks_percent(self, opener):
        self.select_languages(("cs", "pl", "en"))
        opener.side_effect = [
            pageviews_http_response("cs", (10, None, 30, 60)),
            pageviews_http_response("pl", (0, 0, 10, 20)),
            pageviews_http_response("en", (20, 20, 10, 10)),
        ]
        code, compact = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "partial.json"),
        )
        self.assertEqual(code, 0)
        result = self.full(compact)
        self.assertEqual(result["status"], "complete")
        comparison = result["comparison"]
        self.assertEqual(comparison["status"], "available")
        self.assertEqual(comparison["eligible_languages"], ["cs", "en"])
        rows = comparison["rows"]
        self.assertEqual([row["change_percent"] for row in rows], [800.0, None, -50.0])
        self.assertEqual([row["reason"] for row in rows], [None, "zero_baseline", None])
        self.assertEqual(rows[0]["baseline_coverage"]["assumed_zero_days"], 1)
        self.assertEqual(rows[0]["baseline_coverage"]["missing_days"], 0)
        self.assertEqual(rows[0]["baseline_mean_daily_views_observed"], 5.0)
        self.assertEqual(rows[1]["baseline_coverage"]["explicit_zero_days"], 2)
        self.assertEqual(rows[1]["baseline_coverage"]["assumed_zero_days"], 0)
        self.assertEqual(rows[1]["baseline_mean_daily_views_observed"], 0.0)

    @patch("tools.pageviews.client.urlopen")
    def test_empty_series_is_unknown_instead_of_a_zero_series(self, opener):
        opener.return_value = pageviews_http_response("cs", (None, None, None, None))
        code, compact = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "empty.json"),
        )
        self.assertEqual(code, 0)
        result = self.full(compact)
        analysis = result["results"][0]["analysis"]
        self.assertEqual(analysis["status"], "no_observations")
        self.assertIsNone(analysis["current"]["sum_observed_views"])
        self.assertEqual(result["comparison"]["eligible_languages"], [])
        self.assertIsNone(result["comparison"]["rows"][0]["current_mean_daily_views_observed"])

    @patch("tools.pageviews.client.urlopen")
    def test_gap_outside_selected_periods_does_not_make_them_incomplete(self, opener):
        self.select_languages(("cs",))
        opener.return_value = pageviews_http_response("cs", (10, None, 30, 60))
        code, compact = self.invoke(
            "--baseline-end", "2026-07-01", "--user-agent", "study-tests/1",
            "--output", str(self.root / "gap.json"),
        )
        self.assertEqual(code, 0)
        result = self.full(compact)
        self.assertEqual(result["status"], "complete")
        row = result["results"][0]
        self.assertEqual(row["coverage"]["missing_days"], 1)
        self.assertEqual(row["analysis"]["status"], "complete")
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 350.0)
        self.assertEqual(result["comparison"]["status"], "not_computed")

    @patch("tools.pageviews.client.urlopen")
    def test_offline_cache_miss_still_saves_all_language_outcomes(self, opener):
        code, compact = self.invoke("--offline", "--output", str(self.root / "offline.json"))
        self.assertEqual(code, 1)
        result = self.full(compact)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual([row["reason"] for row in result["results"]], ["cache_miss", "no_sitelink"])
        self.assertTrue((self.root / "offline.json").is_file())
        self.assertFalse((self.root / "cache").exists())
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_related_periods_can_reuse_same_outer_window_but_as_of_changes_cache_key(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, compact_first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        first = self.full(compact_first)
        opener.side_effect = AssertionError("Related offline requests must not use HTTP")
        code, compact_related = self.invoke(
            "--offline", "--baseline-end", "2026-07-01", "--monthly",
            "--output", str(self.root / "related.json"),
        )
        self.assertEqual(code, 0)
        related = self.full(compact_related)
        row = related["results"][0]
        self.assertEqual(row["snapshot"], first["results"][0]["snapshot"])
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 350.0)
        self.assertTrue(row["analysis"]["monthly"]["baseline"][0]["partial_calendar_month"])
        code, compact_changed = self.invoke(
            "--offline", "--as-of", "2026-09-26", "--output", str(self.root / "changed.json"),
        )
        self.assertEqual(code, 1)
        changed = self.full(compact_changed)
        self.assertEqual(changed["results"][0]["reason"], "cache_miss")
        opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_reframes_pinned_study_offline_and_report_verifies_broader_snapshot(self, opener):
        self.select_languages(("cs",))
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, compact_original = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "original.json"),
        )
        self.assertEqual(code, 0, compact_original)
        original = self.full(compact_original)
        original_path = Path(compact_original["artifacts"]["study"])
        original_bytes = original_path.read_bytes()

        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--from-study", str(original_path),
                "--baseline-start", "2026-07-02", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-03",
                "--output", str(self.root / "derived.json"),
            ])
        compact_derived = json.loads(output.getvalue())
        self.assertEqual(code, 0, compact_derived)
        derived = self.full(compact_derived)
        self.assertEqual(derived["mode"], "pinned_snapshot_reuse")
        self.assertEqual(derived["periods"]["baseline"], {"start": "2026-07-02", "end": "2026-07-02"})
        row = derived["results"][0]
        self.assertEqual(row["request"], original["results"][0]["request"])
        self.assertEqual(row["snapshot"], original["results"][0]["snapshot"])
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 50.0)
        self.assertEqual(derived["source_study"]["path"], str(original_path))
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(opener.call_count, 1)

        report = build_report(
            Path(compact_derived["artifacts"]["study"]),
            question="Чи змінилися перегляди у вибраних підперіодах?",
        )
        self.assertEqual(report["periods"]["baseline"], {"start": "2026-07-02", "end": "2026-07-02"})
        self.assertEqual(report["evidence"][0]["analysis"]["comparison"]["change_percent"], 50.0)
        self.assertEqual(report["status"], "completed")

        malformed = json.loads(Path(compact_derived["artifacts"]["study"]).read_text())
        del malformed["operation"]
        malformed_ref = save_json_artifact(malformed, self.root / "malformed-derived.json")
        with self.assertRaises(PageviewsError) as caught:
            build_report(malformed_ref.path, question="malformed study reference")
        self.assertEqual(caught.exception.code, "report_source_error")
        self.assertEqual(opener.call_count, 1)

    @patch("tools.pageviews.client.urlopen")
    def test_reframe_rejects_periods_outside_the_pinned_snapshot(self, opener):
        self.select_languages(("cs",))
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, original = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "original.json"),
        )
        self.assertEqual(code, 0, original)
        output_path = self.root / "invalid-derived.json"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--from-study", original["artifacts"]["study"],
                "--baseline-start", "2026-06-30", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-03",
                "--output", str(output_path),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_request")
        self.assertFalse(output_path.exists())
        self.assertEqual(opener.call_count, 1)

    @patch("tools.pageviews.client.urlopen")
    def test_refresh_keeps_old_snapshot_and_records_new_observation(self, opener):
        opener.side_effect = [
            pageviews_http_response("cs", (10, 20, 30, 60)),
            pageviews_http_response("cs", (10, 20, 40, 80)),
        ]
        code, compact_first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        first = self.full(compact_first)
        old = Path(first["results"][0]["artifacts"]["raw"])
        before = old.read_bytes()
        code, compact_refreshed = self.invoke(
            "--refresh", "--user-agent", "study-tests/1",
            "--output", str(self.root / "refreshed.json"),
        )
        self.assertEqual(code, 0)
        refreshed = self.full(compact_refreshed)
        row = refreshed["results"][0]
        self.assertFalse(row["cache_hit"])
        self.assertNotEqual(row["snapshot"], first["results"][0]["snapshot"])
        self.assertEqual(row["analysis"]["comparison"]["change_percent"], 300.0)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(opener.call_count, 2)

        code, compact_latest = self.invoke(
            "--offline", "--output", str(self.root / "latest.json"),
        )
        self.assertEqual(code, 0)
        latest = self.full(compact_latest)
        self.assertTrue(latest["results"][0]["cache_hit"])
        self.assertEqual(latest["results"][0]["snapshot"], row["snapshot"])
        self.assertEqual(latest["results"][0]["analysis"]["comparison"]["change_percent"], 300.0)

        old_study_path = self.root / "first.json"
        historical = build_report(old_study_path, question="Replay pinned historical snapshot")
        self.assertEqual(historical["evidence"][0]["analysis"]["comparison"]["change_percent"], 200.0)
        self.assertEqual(historical["evidence"][0]["source"]["response_sha256"],
                         first["results"][0]["source"]["response_sha256"])
        self.assertEqual(opener.call_count, 2)

    @patch("tools.pageviews.client.urlopen")
    def test_corrupt_cache_is_not_silently_refetched(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        code, compact_first = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "first.json"),
        )
        self.assertEqual(code, 0)
        first = self.full(compact_first)
        Path(first["results"][0]["artifacts"]["raw"]).write_bytes(b"damaged fixture")
        code, compact_second = self.invoke(
            "--user-agent", "study-tests/1", "--output", str(self.root / "second.json"),
        )
        self.assertEqual(code, 1)
        second = self.full(compact_second)
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
                code, compact = self.invoke(
                    "--user-agent", "study-tests/1", "--output", str(self.root / f"limit-{status}.json"),
                )
                self.assertEqual(code, 1)
                result = self.full(compact)
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
                code, compact = self.invoke(
                    "--user-agent", "study-tests/1", "--cache-dir", str(self.root / name),
                    "--output", str(self.root / f"{name}.json"),
                )
                self.assertEqual(code, 1)
                result = self.full(compact)
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
    def test_as_of_defaults_to_today_when_omitted(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        index = self.arguments.index("--as-of")
        del self.arguments[index:index + 2]
        with patch("tools.pageviews.study_cli.datetime") as clock:
            clock.now.return_value.date.return_value.isoformat.return_value = "2026-09-25"
            code, result = self.invoke("--user-agent", "study-tests/1", "--output", str(self.root / "defaulted.json"))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["as_of"], "2026-09-25")

    @patch("tools.pageviews.client.urlopen")
    def test_user_agent_falls_back_to_environment_variable(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        with patch.dict(os.environ, {"TREND_VISOR_USER_AGENT": "study-tests/1 (env fallback)"}):
            code, result = self.invoke("--output", str(self.root / "env-agent.json"))
        self.assertEqual(code, 0, result)
        opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_multiple_resolution_files_are_merged_by_language(self, opener):
        first = save_resolution(resolution_result(("cs", "pl"), unmatched=("pl",)), self.root / "first-res.json")
        second = save_resolution(resolution_result(("pl",), unmatched=()), self.root / "second-res.json")
        opener.side_effect = [
            pageviews_http_response("cs", (10, 20, 30, 60)),
            pageviews_http_response("pl", (100, 200, 200, 400)),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--resolution", str(first.path), "--resolution", str(second.path),
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "merged.json"),
            ])
        compact = json.loads(output.getvalue())
        self.assertEqual(code, 0, compact)
        result = self.full(compact)
        self.assertEqual([row["language"] for row in result["results"]], ["cs", "pl"])
        self.assertEqual([row["status"] for row in result["results"]], ["analyzed", "analyzed"])
        self.assertEqual(len(result["resolutions"]), 2)

    @patch("tools.pageviews.client.urlopen")
    def test_a_language_matched_in_two_resolution_files_is_rejected(self, opener):
        first = save_resolution(resolution_result(("cs",), unmatched=()), self.root / "first-res.json")
        second = save_resolution(resolution_result(("cs",), unmatched=()), self.root / "second-res.json")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--resolution", str(first.path), "--resolution", str(second.path),
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "conflict.json"),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_request")
        self.assertFalse((self.root / "conflict.json").exists())
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_article_flag_collects_pageviews_without_resolution(self, opener):
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--article", f"cs:{TITLES['cs']}",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "by-article.json"),
            ])
        compact = json.loads(output.getvalue())
        self.assertEqual(code, 0, compact)
        result = self.full(compact)
        row = result["results"][0]
        self.assertEqual(row["status"], "analyzed")
        self.assertEqual(row["resolution_status"], "user_confirmed")
        self.assertIsNone(row["entity_id"])
        self.assertEqual(compact["languages"][0]["resolution_status"], "user_confirmed")

    @patch("tools.pageviews.client.urlopen")
    def test_article_combines_with_resolution_for_a_different_language(self, opener):
        resolution = save_resolution(resolution_result(("pl",), unmatched=()), self.root / "pl-only.json")
        opener.side_effect = [
            pageviews_http_response("pl", (100, 200, 200, 400)),
            pageviews_http_response("cs", (10, 20, 30, 60)),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--resolution", str(resolution.path), "--article", f"cs:{TITLES['cs']}",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "combined.json"),
            ])
        compact = json.loads(output.getvalue())
        self.assertEqual(code, 0, compact)
        result = self.full(compact)
        by_language = {row["language"]: row for row in result["results"]}
        self.assertEqual(by_language["pl"]["resolution_status"], "matched")
        self.assertEqual(by_language["cs"]["resolution_status"], "user_confirmed")
        self.assertEqual([row["status"] for row in result["results"]], ["analyzed", "analyzed"])

    @patch("tools.pageviews.client.urlopen")
    def test_article_same_language_as_a_matched_resolution_is_rejected(self, opener):
        resolution = save_resolution(resolution_result(("cs",), unmatched=()), self.root / "cs-res.json")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--resolution", str(resolution.path), "--article", f"cs:{TITLES['cs']}",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "conflict-article.json"),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_request")
        self.assertFalse((self.root / "conflict-article.json").exists())
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_article_with_bad_format_is_rejected_before_network(self, opener):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--article", "no-colon-here",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "bad-article.json"),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_arguments")
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_article_cannot_combine_with_from_study(self, opener):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--from-study", str(self.root / "does-not-matter.json"),
                "--article", f"cs:{TITLES['cs']}",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--output", str(self.root / "article-from-study.json"),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_arguments")
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_no_article_source_at_all_is_rejected(self, opener):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study",
                "--baseline-start", "2026-07-01", "--baseline-end", "2026-07-02",
                "--current-start", "2026-07-03", "--current-end", "2026-07-04",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "no-source.json"),
            ])
        error = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "invalid_arguments")
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_months_aligns_periods_to_full_calendar_months(self, opener):
        self.select_languages(("cs",))
        opener.return_value = pageviews_http_response("cs", (10, 20, 30, 60))
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "study", "--resolution", str(self.saved.path), "--months", "3",
                "--as-of", "2026-09-25", "--cache-dir", str(self.root / "cache"),
                "--user-agent", "study-tests/1", "--output", str(self.root / "months.json"),
            ])
        compact = json.loads(output.getvalue())
        self.assertEqual(code, 0, compact)
        self.assertEqual(compact["periods"]["current"], {"start": "2026-06-01", "end": "2026-08-31"})
        self.assertEqual(compact["periods"]["baseline"], {"start": "2025-06-01", "end": "2025-08-31"})

    @patch("tools.pageviews.client.urlopen")
    def test_unresolved_topic_is_reported_without_pageview_requests(self, opener):
        self.select_languages(("cs", "pl"), unmatched=("cs", "pl"))
        code, compact = self.invoke("--offline", "--output", str(self.root / "unresolved.json"))
        self.assertEqual(code, 0)
        result = self.full(compact)
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
        self.assertIn("--from-study", result.stdout)
        self.assertIn("--offline", result.stdout)