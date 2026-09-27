import json
import unittest
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_study
from tools.pageviews.errors import PageviewsError
from tools.pageviews.reports import MAX_EVIDENCE_BYTES, build_report, evidence_page


class ReportTests(unittest.TestCase):
    def test_single_language_report_does_not_ask_for_cross_language_priorities(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {"cs": (10, 20, 30, 60)}, unmatched=())
            report = build_report(saved.path, question="Чи змінилися перегляди?")
            self.assertEqual(report["prioritization"]["status"], "not_applicable_single_language")
            self.assertIn("лише одну мовну версію", report["prioritization"]["message_uk"])
            self.assertIn("міжмовна пріоритизація не застосовується", report["markdown"])

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_report_uses_the_pinned_snapshot_and_keeps_all_language_outcomes(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, _ = saved_study(root)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report(artifact.path, question="Які аудиторії дослідити далі?")
            self.assertEqual([row["language"] for row in report["evidence"]], ["cs", "pl", "en"])
            first, second, missing = report["evidence"]
            self.assertEqual(first["analysis"]["baseline"]["mean_daily_views_observed"], 15.0)
            self.assertEqual(first["analysis"]["current"]["mean_daily_views_observed"], 45.0)
            self.assertEqual(first["analysis"]["comparison"]["change_percent"], 200.0)
            self.assertEqual(second["analysis"]["comparison"]["change_percent"], 100.0)
            self.assertEqual(missing["reason"], "no_sitelink")
            self.assertIn("calendar", first)
            self.assertIn("diagnostics", first)
            self.assertNotIn("pairs", first["calendar"])
            self.assertIn("зросли з 15,00 до 45,00 на день", report["markdown"])
            self.assertIn("en", report["markdown"])
            self.assertIn("no_sitelink", report["markdown"])
            self.assertEqual(report["prioritization"]["status"], "needs_criteria")
            self.assertEqual(report["report_version"], 5)
            for dimension in ("покриття", "чутливість", "календарна узгодженість"):
                self.assertIn(dimension, report["markdown"])
            self.assertIn("не зводяться до загального бала довіри", report["markdown"])
            page = evidence_page(report)
            self.assertEqual(page["total_rows"], 3)
            self.assertEqual([row["language"] for row in page["evidence"]], ["cs", "pl", "en"])
            self.assertIsNone(page["next_offset"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_absent_days_count_as_zero_but_true_zero_baseline_and_no_data_stay_blocked(self):
        with TemporaryDirectory() as directory:
            artifact, _ = saved_study(Path(directory), {
                "cs": (10, None, 30, 60), "pl": (0, 0, 10, 20), "en": (None,) * 4,
            }, unmatched=())
            report = build_report(artifact.path, question="Порівняй покриття")
            rows = report["evidence"]
            self.assertEqual([row["analysis"]["comparison"]["change_percent"] for row in rows], [800.0, None, None])
            self.assertEqual([row["next_check"]["code"] for row in rows], ["review_page_history", "compare_levels", "review_coverage"])
            self.assertEqual(rows[0]["analysis"]["baseline"]["coverage"]["assumed_zero_days"], 1)
            self.assertEqual(rows[0]["analysis"]["baseline"]["coverage"]["missing_days"], 0)
            self.assertEqual(rows[1]["analysis"]["baseline"]["coverage"]["explicit_zero_days"], 2)
            self.assertIsNone(rows[2]["analysis"]["baseline"]["mean_daily_views_observed"])
            self.assertEqual(rows[2]["analysis"]["status"], "no_observations")
            self.assertIn("невідомо", report["markdown"])
            self.assertIn("zero_baseline", report["markdown"])

    def test_unresolved_only_study_produces_an_explanation_not_empty_report(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {}, unmatched=("cs", "pl"))
            result = build_report(saved.path, question="Що вдалося перевірити?")
            self.assertEqual([row["status"] for row in result["evidence"]], ["not_collected"] * 2)
            self.assertTrue(all(row["next_check"]["code"] == "review_mapping" for row in result["evidence"]))
            self.assertIn("не доказ відсутності інтересу", result["markdown"])
            self.assertIn("2026-07-01", result["markdown"])
            self.assertIn("2026-07-04", result["markdown"])

    def test_collection_failure_is_preserved_but_not_used_as_a_zero_series(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {"cs": (10, 20, 30, 60)}, unmatched=("en",), uncached=("pl",))
            report = build_report(saved.path, question="Порівняй")
            failure = report["evidence"][1]
            self.assertEqual(failure["status"], "collection_failed")
            self.assertEqual(failure["reason"], "cache_miss")
            self.assertNotIn("analysis", failure)
            self.assertEqual(failure["next_check"]["code"], "review_collection")
            self.assertIn("cache_miss", report["markdown"])

    def test_report_identity_is_stable_and_changes_with_question_or_criteria(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            first = build_report(saved.path, question="Порівняй")
            again = build_report(saved.path, question="Порівняй")
            changed = build_report(saved.path, question="Порівняй", criteria=["Повнота"])
            self.assertEqual(first["report_id"], again["report_id"])
            self.assertNotEqual(first["report_id"], changed["report_id"])
            self.assertEqual(
                [{k: v for k, v in row.items() if k != "chart"} for row in first["evidence"]],
                [{k: v for k, v in row.items() if k != "chart"} for row in changed["evidence"]],
            )

    def test_malformed_or_corrupt_study_fails_instead_of_repairing_numbers(self):
        with TemporaryDirectory() as directory:
            saved, study = saved_study(Path(directory))
            mutations = (
                (("schema_version",), True),
                (("study_version",), 99),
                (("operation",), "not-a-study"),
                (("results",), []),
            )
            for keys, value in mutations:
                with self.subTest(keys=keys):
                    changed = deepcopy(study)
                    target = changed
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    saved.path.write_text(json.dumps(changed))
                    with self.assertRaises(PageviewsError) as caught:
                        build_report(saved.path, question="Перевірка")
                    self.assertEqual(caught.exception.code, "report_source_error")
            saved.path.write_text(json.dumps(study))
            Path(study["results"][0]["artifacts"]["raw"]).write_bytes(b"corrupt")
            with self.assertRaises(PageviewsError) as caught:
                build_report(saved.path, question="Перевірка")
            self.assertEqual(caught.exception.code, "report_source_error")

    def test_missing_study_file_is_a_structured_failure(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(PageviewsError) as caught:
                build_report(Path(directory) / "missing.json", question="Перевірка")
            self.assertEqual(caught.exception.code, "report_source_error")

    def test_pages_cover_every_language_and_cannot_mutate_full_report(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            report = build_report(saved.path, question="Порівняй")
            pages = [evidence_page(report, offset=index, limit=1) for index in range(3)]
            self.assertEqual([page["evidence"][0]["language"] for page in pages], ["cs", "pl", "en"])
            self.assertEqual([page["next_offset"] for page in pages], [1, 2, None])
            pages[0]["evidence"][0]["language"] = "changed"
            self.assertEqual(report["evidence"][0]["language"], "cs")
            for options in ({"offset": -1}, {"offset": 3}, {"offset": True}, {"limit": 0}, {"limit": 11}):
                with self.subTest(options=options), self.assertRaises(PageviewsError):
                    evidence_page(report, **options)

    def test_large_evidence_page_is_rejected_without_dropping_fields(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            report = build_report(saved.path, question="𐐀" * 1000, criteria=["𐐀" * 200] * 5)
            with patch("tools.pageviews.reports.MAX_EVIDENCE_BYTES", 12000), self.assertRaises(PageviewsError) as caught:
                evidence_page(report, limit=3)
            self.assertEqual(caught.exception.code, "evidence_too_large")
            self.assertEqual(len(report["evidence"]), 3)

    def test_question_and_criteria_are_literal_input_not_markup_or_ranking(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            question = '<script>bad</script> [link](https://example.invalid)'
            report = build_report(saved.path, question=question, criteria=["Мій критерій"])
            self.assertEqual(report["question"], question)
            self.assertNotIn("<script>", report["markdown"])
            self.assertNotIn("[link](https://example.invalid)", report["markdown"])
            self.assertEqual(report["prioritization"]["status"], "criteria_require_review")
            for invalid in ("", "x" * 1001, "text\n# header", "\ud800"):
                with self.subTest(invalid=repr(invalid)), self.assertRaises(PageviewsError):
                    build_report(saved.path, question=invalid)

    def test_agent_summary_is_labeled_and_not_treated_as_verified_fact(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            report = build_report(saved.path, question="Порівняй", summary="Це власний висновок агента.")
            self.assertEqual(report["summary_uk"], "Це власний висновок агента.")
            self.assertIn("Висновок агента", report["markdown"])
            self.assertIn("Це власний висновок агента", report["markdown"])
            self.assertIn("не перевірений код-факт", report["markdown"])

    @unittest.skipUnless(find_spec("matplotlib") is not None, "Optional charts extra is not installed")
    def test_chart_dir_produces_one_png_per_analyzed_language(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root / "study")
            chart_dir = root / "charts"
            report = build_report(saved.path, question="Порівняй", chart_dir=chart_dir)
            analyzed = [row for row in report["evidence"] if row["status"] == "analyzed"]
            self.assertTrue(analyzed)
            for row in analyzed:
                self.assertTrue(Path(row["chart"]["path"]).is_file())
                self.assertEqual(Path(row["chart"]["path"]).parent, chart_dir.resolve())

    def test_chart_dir_without_matplotlib_fails_closed_instead_of_silently_skipping(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            with patch("tools.pageviews.reports.find_spec", return_value=None):
                with self.assertRaises(PageviewsError) as caught:
                    build_report(saved.path, question="Порівняй", chart_dir=Path(directory) / "charts")
            self.assertEqual(caught.exception.code, "missing_dependency")

    def test_a_two_year_study_still_fits_the_default_evidence_page(self):
        # Regression: the full per-month calendar breakdown used to be embedded
        # verbatim in evidence, pushing even a single language over the byte cap.
        from datetime import date, timedelta
        start = date(2024, 1, 1)
        count = (date(2026, 1, 1) - start).days
        split = count // 2
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(
                Path(directory), {"cs": (10,) * split + (20,) * (count - split)},
                unmatched=(), start=start, split=split,
            )
            report = build_report(saved.path, question="Порівняй за два роки")
            page = evidence_page(report)
            encoded = json.dumps(page, ensure_ascii=False, indent=2).encode()
            self.assertLessEqual(len(encoded), MAX_EVIDENCE_BYTES)
            self.assertNotIn("pairs", report["evidence"][0]["calendar"])
