import json
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_analysis, saved_study
from tests.test_report_criteria import QUESTION, saved_rules
from tools.pageviews.artifacts import JsonArtifact
from tools.pageviews.cli import main
from tools.pageviews.evidence_details import read_evidence_detail
from tools.pageviews.errors import PageviewsError
from tools.pageviews.methodology import MethodologyOptions
from tools.pageviews.research_state import create_research, propose_criteria


class EvidenceDetailTests(unittest.TestCase):
    def state(self, root, study, *, analyses=()):
        return create_research(
            study, question=QUESTION, analysis_artifacts=analyses,
            output=root / "research.json",
        )

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_observations_preserve_missing_zero_and_are_read_only(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, None, 0, 40)}, unmatched=())
            state = self.state(root, study)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            result = read_evidence_detail(
                state, language="cs", kind="observations",
                start="2026-07-01", end="2026-07-04",
            )
            self.assertEqual(result["status"], "available")
            self.assertEqual([row["views"] for row in result["items"]], [10, None, 0, 40])
            self.assertEqual([row["observation"] for row in result["items"]],
                             ["observed", "missing", "observed", "observed"])
            self.assertEqual([row["study_period"] for row in result["items"]],
                             ["baseline", "baseline", "current", "current"])
            self.assertEqual(result["verification"]["status"], "recomputed_and_matched")
            self.assertFalse(result["research"]["rules_evaluated"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_observation_pages_join_without_gaps_or_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            state = self.state(root, study)
            pages = [read_evidence_detail(
                state, language="cs", kind="observations",
                start="2026-07-01", end="2026-07-04", offset=offset, limit=2,
            ) for offset in (0, 2)]
            self.assertEqual([page["page"]["next_offset"] for page in pages], [2, None])
            dates = [row["date"] for page in pages for row in page["items"]]
            self.assertEqual(dates, ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"])
            with self.assertRaises(PageviewsError) as caught:
                read_evidence_detail(state, language="cs", kind="observations",
                                     start="2026-07-01", end="2026-07-04", offset=4)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_missing_dates_can_be_an_available_empty_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, None, 0, 40)}, unmatched=())
            state = self.state(root, study)
            missing = read_evidence_detail(
                state, language="cs", kind="missing_dates",
                start="2026-07-01", end="2026-07-04",
            )
            self.assertEqual(missing["items"], [{"date": "2026-07-02", "study_period": "baseline"}])
            empty = read_evidence_detail(
                state, language="cs", kind="missing_dates",
                start="2026-07-03", end="2026-07-04",
            )
            self.assertEqual(empty["status"], "available")
            self.assertEqual(empty["page"], {"offset": 0, "limit": 50, "returned": 0,
                                             "total": 0, "next_offset": None})

    def test_largest_days_replay_pinned_parameter_and_tie_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, value = saved_study(root, {"cs": (100, 100, 40, 40, 20, 20)},
                                       split=3, unmatched=())
            analysis, _ = saved_analysis(root, value, top_days=2)
            state = self.state(root, study, analyses=(analysis,))
            result = read_evidence_detail(state, language="cs", kind="largest_days", limit=2)
            self.assertEqual(result["parameters"], {"top_days": 2})
            self.assertEqual(result["verification"]["attachment_sha256"], analysis.sha256)
            self.assertEqual(result["items"], [
                {"period": "baseline", "rank": 1, "date": "2026-07-01", "views": 100},
                {"period": "baseline", "rank": 2, "date": "2026-07-02", "views": 100},
            ])
            self.assertEqual(result["page"]["next_offset"], 2)
            second = read_evidence_detail(state, language="cs", kind="largest_days", offset=2, limit=2)
            self.assertEqual([row["period"] for row in second["items"]], ["current", "current"])

    def test_largest_days_pages_reconstruct_more_than_saved_preview(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            values = tuple(range(40))
            study, value = saved_study(root, {"cs": values}, split=20, unmatched=())
            analysis, recorded = saved_analysis(root, value, top_days=12)
            self.assertTrue(recorded["diagnostics"]["largest_days"]["baseline"]["top_dates_truncated"])
            state = self.state(root, study, analyses=(analysis,))
            pages = []
            offset = 0
            while True:
                page = read_evidence_detail(
                    state, language="cs", kind="largest_days", offset=offset, limit=5,
                )
                pages.extend(page["items"])
                if page["page"]["next_offset"] is None:
                    break
                offset = page["page"]["next_offset"]
            self.assertEqual(len(pages), 24)
            self.assertEqual(len({(row["period"], row["date"]) for row in pages}), 24)
            self.assertEqual([row["views"] for row in pages[:3]], [19, 18, 17])
            self.assertEqual([row["views"] for row in pages[12:15]], [39, 38, 37])

    def test_recorded_monthly_and_calendar_details_are_available(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2025, 1, 1)
            values = (10,) * 365 + (20,) * 365
            study, _ = saved_study(
                root, {"cs": values}, unmatched=(), start=start, split=365,
                monthly=True, methodology=MethodologyOptions(),
            )
            state = self.state(root, study)
            monthly = read_evidence_detail(state, language="cs", kind="monthly_summaries", limit=24)
            self.assertEqual(monthly["page"]["total"], 24)
            self.assertEqual(monthly["items"][0]["period"], "baseline")
            self.assertEqual(monthly["items"][-1]["period"], "current")
            calendar = read_evidence_detail(state, language="cs", kind="calendar_comparisons", limit=8)
            self.assertEqual(calendar["page"]["total"], 12)
            self.assertEqual(calendar["summary"]["computed_pairs"], 12)
            self.assertEqual(calendar["items"][0]["status"], "computed")
            self.assertEqual(calendar["items"][0]["change_percent"], 100.0)
            self.assertEqual(calendar["page"]["next_offset"], 8)

    def test_calendar_details_keep_leap_days_and_partial_month_reasons(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2024, 1, 1)
            study, _ = saved_study(
                root, {"cs": (10,) * 366 + (20,) * 365}, unmatched=(), start=start,
                split=366, monthly=True, methodology=MethodologyOptions(),
            )
            state = self.state(root, study)
            monthly = read_evidence_detail(state, language="cs", kind="monthly_summaries", limit=24)
            february = next(row for row in monthly["items"] if row["month"] == "2024-02")
            self.assertEqual(february["coverage"]["expected_days"], 29)
            calendar = read_evidence_detail(state, language="cs", kind="calendar_comparisons", limit=8)
            february_pair = next(row for row in calendar["items"] if row["current_month"] == "2025-02")
            self.assertEqual(february_pair["baseline"]["coverage"]["expected_days"], 29)
            self.assertEqual(february_pair["current"]["coverage"]["expected_days"], 28)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2024, 1, 15)
            study, _ = saved_study(
                root, {"cs": (10,) * 366 + (20,) * 365}, unmatched=(), start=start,
                split=366, monthly=True, methodology=MethodologyOptions(),
            )
            state = self.state(root, study)
            calendar = read_evidence_detail(state, language="cs", kind="calendar_comparisons", limit=8)
            first = calendar["items"][0]
            self.assertEqual(first["status"], "not_computed")
            self.assertEqual(first["reason"], "partial_calendar_month")
            self.assertIn("current", first["affected_periods"])

    def test_unrecorded_optional_details_are_explicitly_unavailable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            state = self.state(root, study)
            expected = {
                "largest_days": "diagnostics_not_supplied",
                "monthly_summaries": "monthly_not_recorded",
                "calendar_comparisons": "calendar_comparison_not_recorded",
            }
            for kind, reason in expected.items():
                with self.subTest(kind=kind):
                    result = read_evidence_detail(state, language="cs", kind=kind)
                    self.assertEqual(result["status"], "unavailable")
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(result["items"], [])

    def test_unanalyzed_language_is_unavailable_but_unknown_language_is_invalid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=("en",))
            state = self.state(root, study)
            result = read_evidence_detail(
                state, language="en", kind="observations",
                start="2026-07-01", end="2026-07-04",
            )
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "language_not_analyzed")
            with self.assertRaises(PageviewsError) as caught:
                read_evidence_detail(state, language="uk", kind="observations",
                                     start="2026-07-01", end="2026-07-04")
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_pending_state_exposes_facts_without_evaluating_rules(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            rules, _ = saved_rules(root, study)
            initial = self.state(root, study)
            pending = propose_criteria(initial, rules, output=root / "pending.json")
            result = read_evidence_detail(
                pending, language="cs", kind="observations",
                start="2026-07-01", end="2026-07-01",
            )
            self.assertEqual(result["research"]["status"], "awaiting_confirmation")
            self.assertFalse(result["research"]["rules_evaluated"])
            self.assertEqual(result["limitations"], ["pending_rules_not_evaluated"])
            self.assertNotIn("criteria_evaluation", json.dumps(result))

    def test_invalid_ranges_types_limits_and_non_range_dates_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            state = self.state(root, study)
            cases = (
                {"kind": "observations", "start": "2026-07-01"},
                {"kind": "observations", "start": "2026-07-03", "end": "2026-07-02"},
                {"kind": "observations", "start": "2026-06-30", "end": "2026-07-01"},
                {"kind": "largest_days", "start": "2026-07-01"},
                {"kind": "observations", "start": "2026-07-01", "end": "2026-07-01", "offset": True},
                {"kind": "observations", "start": "2026-07-01", "end": "2026-07-01", "limit": True},
                {"kind": "observations", "start": "2026-07-01", "end": "2026-07-01", "limit": 101},
                {"kind": "unknown"},
            )
            for options in cases:
                with self.subTest(options=options), self.assertRaises(PageviewsError) as caught:
                    read_evidence_detail(state, language="cs", **options)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_oversized_page_is_rejected_without_silent_truncation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            state = self.state(root, study)
            with patch("tools.pageviews.reports.MAX_EVIDENCE_BYTES", 100), self.assertRaises(PageviewsError) as caught:
                read_evidence_detail(
                    state, language="cs", kind="observations",
                    start="2026-07-01", end="2026-07-04",
                )
            self.assertEqual(caught.exception.code, "evidence_too_large")
            self.assertIn("reduce the page size", str(caught.exception))

    def test_changed_state_snapshot_and_attachment_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, value = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            analysis, result = saved_analysis(root, value)
            state = self.state(root, study, analyses=(analysis,))
            with self.assertRaises(PageviewsError):
                read_evidence_detail(JsonArtifact(state.path, "0" * 64), language="cs",
                                     kind="observations", start="2026-07-01", end="2026-07-01")
            Path(value["results"][0]["artifacts"]["raw"]).write_bytes(b"changed")
            with self.assertRaises(PageviewsError):
                read_evidence_detail(state, language="cs", kind="observations",
                                     start="2026-07-01", end="2026-07-01")

            other = Path(directory) / "other"
            other.mkdir()
            study, value = saved_study(other, {"cs": (10, 20, 30, 40)}, unmatched=())
            analysis, result = saved_analysis(other, value)
            state = self.state(other, study, analyses=(analysis,))
            analysis.path.write_bytes(analysis.path.read_bytes() + b"\n")
            with self.assertRaises(PageviewsError) as caught:
                read_evidence_detail(state, language="cs", kind="largest_days")
            self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_research_detail_cli_matches_public_interface(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, None, 0, 40)}, unmatched=())
            state = self.state(root, study)
            arguments = [
                "research", "detail", "--state", str(state.path), state.sha256,
                "--language", "cs", "--kind", "missing_dates",
                "--start", "2026-07-01", "--end", "2026-07-04",
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(arguments)
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["items"], [{"date": "2026-07-02", "study_period": "baseline"}])
            self.assertNotIn("output", result)

    def test_research_detail_cli_runs_without_optional_dependencies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, 30, 40)}, unmatched=())
            state = self.state(root, study)
            result = subprocess.run(
                [sys.executable, "-S", "-m", "tools.pageviews", "research", "detail",
                 "--state", str(state.path), state.sha256, "--language", "cs",
                 "--kind", "observations", "--start", "2026-07-01", "--end", "2026-07-01"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["items"][0]["views"], 10)
