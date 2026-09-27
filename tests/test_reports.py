import hashlib
import json
import unittest
from copy import deepcopy
from datetime import date
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_study
from tools.pageviews.errors import PageviewsError
from tools.pageviews.methodology import MethodologyOptions
from tools.pageviews.reports import MAX_EVIDENCE_BYTES, build_report, evidence_page


class ReportTests(unittest.TestCase):
    def test_single_language_report_does_not_ask_for_cross_language_priorities(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {"cs": (10, 20, 30, 60)}, unmatched=())
            report = build_report(saved.path, saved.sha256, question="Чи змінилися перегляди?")
            self.assertEqual(report["prioritization"]["status"], "not_applicable_single_language")
            self.assertIn("лише одну мовну версію", report["prioritization"]["message_uk"])
            self.assertIn("міжмовна пріоритизація не застосовується", report["markdown"])
            self.assertNotIn("Пріоритет мов не визначено", report["markdown"])

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_report_uses_verified_study_and_keeps_all_language_outcomes(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact, _ = saved_study(root)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report(artifact.path, artifact.sha256, question="Які аудиторії дослідити далі?")
            self.assertEqual([row["language"] for row in report["evidence"]], ["cs", "pl", "en"])
            first, second, missing = report["evidence"]
            self.assertEqual(first["analysis"]["baseline"]["mean_daily_views_observed"], 15.0)
            self.assertEqual(first["analysis"]["current"]["mean_daily_views_observed"], 45.0)
            self.assertEqual(first["analysis"]["comparison"]["change_percent"], 200.0)
            self.assertEqual(second["analysis"]["comparison"]["change_percent"], 100.0)
            self.assertEqual(missing["reason"], "no_sitelink")
            self.assertIn("зросли з 15,00 до 45,00 на день", report["markdown"])
            self.assertIn("en", report["markdown"])
            self.assertIn("no_sitelink", report["markdown"])
            self.assertEqual(report["prioritization"]["status"], "needs_criteria")
            self.assertEqual(report["report_version"], 4)
            self.assertTrue(any("Єдиного показника довіри" in item and "статистичною впевненістю" in item
                                for item in report["limitations_uk"]))
            for dimension in ("цілісність", "покриття", "чутливість", "календарна узгодженість", "статистичний висновок"):
                self.assertIn(dimension, report["markdown"])
            self.assertIn("не зводяться до загального бала довіри", report["markdown"])
            page = evidence_page(report)
            self.assertEqual(page["total_rows"], 3)
            self.assertEqual(page["next_offset"], 1)
            self.assertEqual([row["language"] for row in page["evidence"]], ["cs"])
            for key in ("markdown", "monthly", "days", "snapshot", "mapping"):
                self.assertNotIn('"' + key + '":', json.dumps(page))
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_missing_zero_and_no_observations_do_not_become_invented_changes(self):
        with TemporaryDirectory() as directory:
            artifact, _ = saved_study(Path(directory), {
                "cs": (10, None, 30, 60), "pl": (0, 0, 10, 20), "en": (None,) * 4,
            }, unmatched=())
            report = build_report(artifact.path, artifact.sha256, question="Порівняй покриття")
            rows = report["evidence"]
            self.assertEqual([row["analysis"]["comparison"]["change_percent"] for row in rows], [None] * 3)
            self.assertEqual([row["next_check"]["code"] for row in rows], ["review_coverage", "compare_levels", "review_coverage"])
            self.assertEqual(rows[0]["analysis"]["baseline"]["coverage"]["missing_days"], 1)
            self.assertEqual(rows[1]["analysis"]["baseline"]["coverage"]["explicit_zero_days"], 2)
            self.assertIsNone(rows[2]["analysis"]["baseline"]["mean_daily_views_observed"])
            self.assertIn("невідомо", report["markdown"])
            self.assertIn("zero_baseline", report["markdown"])

    def test_unresolved_only_study_produces_an_explanation_not_empty_report(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {}, unmatched=("cs", "pl"))
            result = build_report(saved.path, saved.sha256, question="Що вдалося перевірити?")
            self.assertEqual([row["status"] for row in result["evidence"]], ["not_collected"] * 2)
            self.assertTrue(all(row["next_check"]["code"] == "review_mapping" for row in result["evidence"]))
            self.assertIn("не доказ відсутності інтересу", result["markdown"])
            self.assertIn("2026-07-01", result["markdown"])
            self.assertIn("2026-07-04", result["markdown"])

    def test_collection_failure_is_preserved_but_not_used_as_a_zero_series(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), {"cs": (10, 20, 30, 60)}, unmatched=("en",), uncached=("pl",))
            report = build_report(saved.path, saved.sha256, question="Порівняй")
            failure = report["evidence"][1]
            self.assertEqual(failure["status"], "collection_failed")
            self.assertEqual(failure["reason"], "cache_miss")
            self.assertNotIn("analysis", failure)
            self.assertEqual(failure["next_check"]["code"], "review_collection")
            self.assertIn("cache_miss", report["markdown"])

    def test_report_identity_and_text_are_stable_until_user_inputs_change(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            first = build_report(saved.path, saved.sha256, question="Порівняй")
            again = build_report(saved.path, saved.sha256, question="Порівняй")
            changed = build_report(saved.path, saved.sha256, question="Порівняй", criteria=["Повнота"])
            self.assertEqual(first, again)
            self.assertNotEqual(first["report_id"], changed["report_id"])
            self.assertEqual(first["evidence"], changed["evidence"])

    def test_signed_but_inconsistent_study_fails_instead_of_repairing_numbers(self):
        mutations = (
            (("schema_version",), True),
            (("results", 0, "analysis", "baseline", "sum_observed_views"), 999),
            (("results", 0, "analysis", "comparison", "change_percent"), 999),
            (("results", 0, "source", "response_sha256"), "0" * 64),
            (("results", 0, "article"), "Different article"),
            (("results", 2, "status"), "analyzed"),
            (("results",), []),
            (("resolution", "entity", "entity_id"), "Q1"),
        )
        with TemporaryDirectory() as directory:
            saved, study = saved_study(Path(directory))
            for keys, value in mutations:
                with self.subTest(keys=keys):
                    changed = deepcopy(study)
                    target = changed
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    body = json.dumps(changed).encode()
                    saved.path.write_bytes(body)
                    with self.assertRaises(PageviewsError) as caught:
                        build_report(saved.path, hashlib.sha256(body).hexdigest(), question="Перевірка")
                    self.assertEqual(caught.exception.code, "report_source_error")

    def test_checksum_json_and_snapshot_corruption_are_structured_failures(self):
        with TemporaryDirectory() as directory:
            saved, study = saved_study(Path(directory))
            for checksum in ("é" * 64, "0" * 64):
                with self.subTest(checksum=checksum), self.assertRaises(PageviewsError):
                    build_report(saved.path, checksum, question="Перевірка")
            original = saved.path.read_bytes()
            for body in (b"[]", b"{bad", b'{"extra":NaN,' + original[1:], b'{"operation":"study",' + original[1:]):
                with self.subTest(body=body[:30]), self.assertRaises(PageviewsError):
                    saved.path.write_bytes(body)
                    build_report(saved.path, hashlib.sha256(body).hexdigest(), question="Перевірка")
            saved.path.write_bytes(original)
            Path(study["results"][0]["artifacts"]["raw"]).write_bytes(b"corrupt")
            with self.assertRaises(PageviewsError) as caught:
                build_report(saved.path, saved.sha256, question="Перевірка")
            self.assertEqual(caught.exception.code, "report_source_error")

    def test_pages_cover_every_language_and_cannot_mutate_full_report(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            report = build_report(saved.path, saved.sha256, question="Порівняй")
            pages = [evidence_page(report, offset=index) for index in range(3)]
            self.assertEqual([page["evidence"][0]["language"] for page in pages], ["cs", "pl", "en"])
            self.assertEqual([page["next_offset"] for page in pages], [1, 2, None])
            pages[0]["evidence"][0]["language"] = "changed"
            self.assertEqual(report["evidence"][0]["language"], "cs")
            for options in ({"offset": -1}, {"offset": 3}, {"offset": True}, {"limit": 0}, {"limit": 4}):
                with self.subTest(options=options), self.assertRaises(PageviewsError):
                    evidence_page(report, **options)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_eight_year_data_volume_does_not_expand_evidence_like_daily_records(self, network):
        sizes = []
        with TemporaryDirectory() as directory:
            for years in (1, 2, 5, 8):
                start = date(2018, 1, 1)
                count = (date(2018 + years, 1, 1) - start).days
                split = count // 2
                saved, _ = saved_study(Path(directory) / str(years), {"cs": (10,) * split + (20,) * (count - split)},
                                       unmatched=(), start=start, split=split, monthly=True, methodology=MethodologyOptions())
                report = build_report(saved.path, saved.sha256, question="Порівняй вибрані періоди")
                row = report["evidence"][0]
                self.assertEqual(row["analysis"]["comparison"]["change_percent"], 100.0)
                self.assertEqual(row["analysis"]["baseline"]["sum_observed_views"], split * 10)
                page = evidence_page(report)
                encoded = json.dumps(page, ensure_ascii=False).encode()
                sizes.append(len(encoded))
                self.assertLessEqual(len(encoded), MAX_EVIDENCE_BYTES)
                for key in ("days", "monthly", "pairs", "residuals"):
                    self.assertNotIn('"' + key + '":', encoded.decode())
        self.assertLess(max(sizes) - min(sizes), 300)
        network.assert_not_called()

    def test_large_evidence_page_is_rejected_without_dropping_fields(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            report = build_report(saved.path, saved.sha256, question="𐐀" * 1000, criteria=["𐐀" * 200] * 5)
            with patch("tools.pageviews.reports.MAX_EVIDENCE_BYTES", 12000), self.assertRaises(PageviewsError) as caught:
                evidence_page(report, limit=3)
            self.assertEqual(caught.exception.code, "evidence_too_large")
            self.assertEqual(len(report["evidence"]), 3)

    def test_question_and_criteria_are_literal_input_not_markup_or_ranking(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory))
            question = '<script>bad</script> [link](https://example.invalid)'
            report = build_report(saved.path, saved.sha256, question=question, criteria=["Мій критерій"])
            self.assertEqual(report["question"], question)
            self.assertNotIn("<script>", report["markdown"])
            self.assertNotIn("[link](https://example.invalid)", report["markdown"])
            self.assertEqual(report["prioritization"]["status"], "criteria_require_review")
            for invalid in ("", "x" * 1001, "text\n# header", "\ud800"):
                with self.subTest(invalid=repr(invalid)), self.assertRaises(PageviewsError):
                    build_report(saved.path, saved.sha256, question=invalid)

    @unittest.skipUnless(find_spec("statsmodels") is not None, "Optional statistics extra is not installed")
    def test_recorded_model_is_not_refitted_or_claimed_verified_by_text_report(self):
        with TemporaryDirectory() as directory:
            saved, _ = saved_study(Path(directory), methodology=MethodologyOptions("linear-calendar-hac", 0))
            with patch("tools.pageviews.trend_model.require_statistics", side_effect=AssertionError("No refit")):
                report = build_report(saved.path, saved.sha256, question="Які висновки доступні?")
            self.assertEqual(report["evidence"][0]["recorded_model"], "not_revalidated_not_reported")
            self.assertIn("не переоцінює", report["markdown"])
            self.assertIn("збережена модель не переоцінена й не наведена", report["markdown"])
            self.assertNotIn("confidence_interval", json.dumps(evidence_page(report)))
