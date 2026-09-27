import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_analysis, saved_chart, saved_study
from tools.pageviews.artifacts import JsonArtifact, save_json_artifact
from tools.pageviews.cli import main
from tools.pageviews.errors import PageviewsError
from tools.pageviews.reports import build_report, evidence_json, evidence_page


class ReportDiagnosticsTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_verified_diagnostics_keep_original_change_and_separate_scenarios(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, split=4, unmatched=())
            analysis, _ = saved_analysis(root, study)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report(saved.path, saved.sha256, question="Наскільки зміна залежить від піків?",
                                  analysis_artifacts=(analysis,))
            row = report["evidence"][0]
            self.assertEqual(row["analysis"]["comparison"]["change_percent"], 12.5)
            self.assertEqual(row["diagnostics"]["largest_days"]["comparison_after_exclusion"]["change_percent"], 100.0)
            self.assertEqual(row["diagnostics"]["window_edges"]["scenario"]["comparison"]["change_percent"], 80.0)
            self.assertEqual(row["additional_evidence"], {"diagnostics": "verified", "chart": "not_supplied"})
            self.assertEqual(report["verification"]["diagnostics"], "recomputed_and_matched")
            self.assertEqual(report["audit"]["attachments"][0]["sha256"], analysis.sha256)
            self.assertIn("без 1 найбільших днів", report["markdown"])
            self.assertIn("+100,00%", report["markdown"])
            self.assertIn("+80,00%", report["markdown"])
            self.assertIn("не довірчі інтервали", report["markdown"])
            page = evidence_page(report)
            self.assertNotIn("top_days", page["evidence"][0]["diagnostics"]["largest_days"]["baseline"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_missing_value_assumptions_and_unavailable_scenarios_are_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root, {"cs": (10, 20, None, 10)}, unmatched=())
            analysis, _ = saved_analysis(root, study, upper_bound=50)
            report = build_report(saved.path, saved.sha256, question="Що означають пропуски?", analysis_artifacts=(analysis,))
            row = report["evidence"][0]
            self.assertIsNone(row["analysis"]["comparison"]["change_percent"])
            missing = row["diagnostics"]["missing_values"]
            self.assertEqual(missing["break_even"]["required_missing_views_total"], 20)
            self.assertEqual(missing["conditional_bounds"]["lower_change_percent"], -66.666667)
            self.assertEqual(missing["conditional_bounds"]["upper_change_percent"], 100.0)
            self.assertIn("від 0 до 50", report["markdown"])
            self.assertIn("вимога, не оцінка", report["markdown"])
            self.assertEqual(row["diagnostics"]["window_edges"]["reason"], "period_too_short")
            self.assertIn("Обрізання країв не виконано", report["markdown"])
            self.assertEqual(row["diagnostics"]["window_edges"]["scenario"], None)

    def test_trimmed_complete_window_does_not_repair_original_missing_days(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root, {"cs": (10, 20, 30, 40, None, 40, 50, 60)}, unmatched=(), split=4)
            analysis, _ = saved_analysis(root, study)
            report = build_report(saved.path, saved.sha256, question="Перевір краї вікон", analysis_artifacts=(analysis,))
            row = report["evidence"][0]
            self.assertEqual(row["analysis"]["comparison"]["reason"], "incomplete_coverage")
            self.assertIsNone(row["analysis"]["comparison"]["change_percent"])
            self.assertEqual(row["diagnostics"]["window_edges"]["scenario"]["comparison"]["change_percent"], 80.0)
            self.assertIn("не обчислено", report["markdown"])
            self.assertIn("+80,00%", report["markdown"])
            self.assertEqual(row["diagnostics"]["missing_values"]["conditional_bounds"]["reason"], "missing_upper_bound_required")
            self.assertIn("Межі за припущенням про пропуски не обчислено", report["markdown"])

    def test_rehashed_wrong_diagnostics_scope_parameters_and_values_are_rejected(self):
        mutations = (
            (("operation",), "chart"), (("schema_version",), True),
            (("source", "response_sha256"), "0" * 64),
            (("request", "article"), "Other_article"),
            (("baseline", "window", "end"), "2026-07-02"),
            (("comparison", "change_percent"), 999),
            (("diagnostics", "parameters", "top_days"), True),
            (("diagnostics", "parameters", "trim_days"), -1),
            (("diagnostics", "largest_days", "comparison_after_exclusion", "change_percent"), 999),
            (("diagnostics", "window_edges", "scenario", "comparison", "change_percent"), 999),
            (("diagnostics",), None),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, split=4, unmatched=())
            _, original = saved_analysis(root, study)
            for index, (keys, value) in enumerate(mutations):
                with self.subTest(keys=keys):
                    changed = deepcopy(original)
                    target = changed
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    artifact = save_json_artifact(changed, root / f"wrong-{index}.json")
                    with self.assertRaises(PageviewsError) as caught:
                        build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(artifact,))
                    self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_same_article_different_snapshot_duplicate_and_invalid_json_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            analysis, original = saved_analysis(root, study)
            _, other_study = saved_study(root / "other")
            other, _ = saved_analysis(root / "other", other_study)
            for refs in ((analysis, analysis), (other,), (JsonArtifact(analysis.path, "0" * 64),)):
                with self.subTest(refs=refs), self.assertRaises(PageviewsError) as caught:
                    build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=refs)
                self.assertEqual(caught.exception.code, "report_attachment_error")
            for checksum in ("é" * 64, "A" * 64, "0"):
                with self.subTest(checksum=checksum), self.assertRaises(PageviewsError) as caught:
                    build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(JsonArtifact(analysis.path, checksum),))
                self.assertEqual(caught.exception.code, "invalid_request")
            valid = json.dumps(original).encode()
            for body in (b"[]", b"{bad", b'{"schema_version":1,' + valid[1:], b'{"extra":NaN,' + valid[1:]):
                analysis.path.write_bytes(body)
                ref = JsonArtifact(analysis.path, hashlib.sha256(body).hexdigest())
                with self.subTest(body=body[:20]), self.assertRaises(PageviewsError) as caught:
                    build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(ref,))
                self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_attachment_order_does_not_change_identity_and_no_attachment_is_invented(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            cs, _ = saved_analysis(root, study)
            pl, _ = saved_analysis(root, study, row_index=1)
            report = build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(cs, pl))
            again = build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(pl, cs))
            plain = build_report(saved.path, saved.sha256, question="Перевір")
            self.assertEqual(report, again)
            self.assertNotEqual(report["report_id"], plain["report_id"])
            self.assertEqual(report["evidence"][2]["additional_evidence"], {"diagnostics": "not_supplied", "chart": "not_supplied"})
            self.assertTrue(all(row["additional_evidence"]["diagnostics"] == "not_supplied" for row in plain["evidence"]))

    def test_attachment_cli_failure_leaves_no_report_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            analysis, _ = saved_analysis(root, study)
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["report", "--study", str(saved.path), "--study-sha256", saved.sha256, "--question", "Перевір",
                             "--analysis-result", str(analysis.path), "0" * 64,
                             "--output", str(root / "report.json"), "--markdown", str(root / "report.md")])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stream.getvalue())["error"]["code"], "report_attachment_error")
            self.assertFalse((root / "report.json").exists())
            self.assertFalse((root / "report.md").exists())

    def test_eight_year_diagnostics_remain_compact_without_day_lists(self):
        sizes = []
        with TemporaryDirectory() as directory:
            for years in (1, 8):
                root = Path(directory) / str(years)
                start = date(2018, 1, 1)
                count = (date(2018 + years, 1, 1) - start).days
                split = count // 2
                saved, study = saved_study(root, {"cs": (10,) * split + (20,) * (count - split)},
                                          unmatched=(), start=start, split=split)
                analysis, _ = saved_analysis(root, study, top_days=20)
                report = build_report(saved.path, saved.sha256, question="Перевір", analysis_artifacts=(analysis,))
                page = evidence_page(report)
                sizes.append(len(evidence_json(page).encode()))
                self.assertEqual(page["evidence"][0]["diagnostics"]["largest_days"]["comparison_after_exclusion"]["change_percent"], 100.0)
                self.assertNotIn('"date":', evidence_json(page))
                self.assertNotIn('"days":', evidence_json(page))
        self.assertLess(max(sizes), 12000)
        self.assertLess(max(sizes) - min(sizes), 300)

    def test_diagnostics_report_works_without_optional_dependencies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            analysis, _ = saved_analysis(root, study)
            result = subprocess.run(
                [sys.executable, "-S", "-m", "tools.pageviews", "report", "--study", str(saved.path),
                 "--study-sha256", saved.sha256, "--question", "Перевір", "--analysis-result", str(analysis.path), analysis.sha256],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["verification"]["diagnostics"], "recomputed_and_matched")


@unittest.skipUnless(find_spec("matplotlib") is not None, "Optional charts extra is not installed")
class ReportChartTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_chart_is_bound_to_same_source_periods_and_png_metadata(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            chart, result = saved_chart(root, study)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report(saved.path, saved.sha256, question="Покажи графік", chart_artifacts=(chart,))
            attached = report["evidence"][0]["chart"]
            self.assertEqual(attached["sha256"], result["chart_sha256"])
            self.assertEqual(attached["path"], result["artifacts"]["chart"])
            self.assertEqual(report["evidence"][0]["additional_evidence"]["chart"], "verified")
            self.assertIn("checksum_metadata_and_source_matched", report["verification"]["charts"])
            self.assertIn(Path(attached["path"]).as_uri(), report["markdown"])
            self.assertEqual(report["audit"]["attachments"][0]["png_sha256"], result["chart_sha256"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_cli_attaches_verified_results_and_publishes_text_with_graph_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, split=4, unmatched=())
            analysis, _ = saved_analysis(root, study)
            chart, result = saved_chart(root, study)
            output, markdown = root / "report.json", root / "report.md"
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main([
                    "report", "--study", str(saved.path), "--study-sha256", saved.sha256, "--question", "Перевір чутливість",
                    "--analysis-result", str(analysis.path), analysis.sha256,
                    "--chart-result", str(chart.path), chart.sha256,
                    "--output", str(output), "--markdown", str(markdown),
                ])
            self.assertEqual(code, 0, stream.getvalue())
            compact = json.loads(stream.getvalue())
            full = json.loads(output.read_text())
            self.assertEqual(markdown.read_text(), full["markdown"])
            self.assertEqual(compact["evidence"][0]["additional_evidence"], {"diagnostics": "verified", "chart": "verified"})
            self.assertIn(Path(result["artifacts"]["chart"]).as_uri(), markdown.read_text())
            self.assertLessEqual(len(stream.getvalue().encode()), 12000)
            self.assertEqual(len(full["audit"]["attachments"]), 2)

    def test_chart_metadata_rejects_other_language_even_after_rehashing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            _, original = saved_chart(root, study)
            _, other = saved_chart(root, study, row_index=1)
            changed = deepcopy(original)
            changed["artifacts"]["chart"] = other["artifacts"]["chart"]
            changed["chart_sha256"] = other["chart_sha256"]
            artifact = save_json_artifact(changed, root / "swapped.json")
            with self.assertRaises(PageviewsError) as caught:
                build_report(saved.path, saved.sha256, question="Перевір", chart_artifacts=(artifact,))
            self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_wrong_chart_periods_method_and_rehashed_truncated_png_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            _, original = saved_chart(root, study)
            mutations = ((("periods", "baseline", "window", "end"), "2026-07-04"),
                         (("method", "smoothing"), "moving_average"), (("chart_version",), True))
            for index, (keys, value) in enumerate(mutations):
                changed = deepcopy(original)
                target = changed
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                artifact = save_json_artifact(changed, root / f"bad-{index}.json")
                with self.subTest(keys=keys), self.assertRaises(PageviewsError) as caught:
                    build_report(saved.path, saved.sha256, question="Перевір", chart_artifacts=(artifact,))
                self.assertEqual(caught.exception.code, "report_attachment_error")
            png = Path(original["artifacts"]["chart"])
            png.write_bytes(png.read_bytes()[:100])
            original["chart_sha256"] = hashlib.sha256(png.read_bytes()).hexdigest()
            artifact = save_json_artifact(original, root / "truncated.json")
            with self.assertRaises(PageviewsError) as caught:
                build_report(saved.path, saved.sha256, question="Перевір", chart_artifacts=(artifact,))
            self.assertEqual(caught.exception.code, "report_attachment_error")

    def test_missing_png_or_missing_optional_dependency_cannot_publish_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, study = saved_study(root)
            chart, result = saved_chart(root, study)
            response = subprocess.run(
                [sys.executable, "-S", "-m", "tools.pageviews", "report", "--study", str(saved.path),
                 "--study-sha256", saved.sha256, "--question", "Перевір", "--chart-result", str(chart.path), chart.sha256,
                 "--output", str(root / "report.json")],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False,
            )
            self.assertEqual(response.returncode, 1)
            self.assertEqual(json.loads(response.stdout)["error"]["code"], "missing_dependency")
            self.assertFalse((root / "report.json").exists())
            Path(result["artifacts"]["chart"]).unlink()
            with self.assertRaises(PageviewsError) as caught:
                build_report(saved.path, saved.sha256, question="Перевір", chart_artifacts=(chart,))
            self.assertEqual(caught.exception.code, "report_attachment_error")


