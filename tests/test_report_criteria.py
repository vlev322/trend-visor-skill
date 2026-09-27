import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_analysis, saved_chart, saved_study
from tools.pageviews.artifacts import JsonArtifact, save_json_artifact
from tools.pageviews.cli import main
from tools.pageviews.errors import PageviewsError
from tools.pageviews.reports import MAX_EVIDENCE_BYTES, build_report, evidence_page

QUESTION = "Які аудиторії перевірити за погодженим критерієм?"


def rule(metric="change_percent", threshold=150, *, operator="gte", identifier="growth", parameters=None):
    return {"id": identifier, "description": "Погоджена умова для перевірки", "metric": metric,
            "operator": operator, "threshold": threshold, "parameters": parameters or {}}


def saved_rules(root, study, rules=None, *, match="all", name="rules.json"):
    document = {"criteria_version": 1, "study_sha256": study.sha256, "question": QUESTION,
                "match": match, "rules": [rule()] if rules is None else rules}
    return save_json_artifact(document, root / name), document


class ReportCriteriaTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_explicit_threshold_selects_followups_without_ranking_or_hiding_unknowns(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)
            self.assertEqual(report["prioritization"]["status"], "evaluated")
            self.assertEqual(report["prioritization"]["counts"], {"matches": 1, "does_not_match": 1, "undetermined": 1})
            decisions = [row["criteria_evaluation"] for row in report["evidence"]]
            self.assertEqual([row["status"] for row in decisions], ["matches", "does_not_match", "undetermined"])
            self.assertEqual([row["checks"][0]["value"] for row in decisions], [200.0, 100.0, None])
            self.assertEqual(decisions[2]["checks"][0]["reason"], "no_sitelink")
            self.assertEqual(decisions[0]["checks"][0]["evidence_id"], report["evidence"][0]["evidence_id"])
            self.assertEqual(report["audit"]["criteria_rules"]["sha256"], rules.sha256)
            self.assertIn("Кандидат для подальшої перевірки", report["markdown"])
            self.assertIn("Не відповідає погодженому правилу", report["markdown"])
            self.assertIn("Недостатньо даних", report["markdown"])
            self.assertIn("150", report["markdown"])
            self.assertIn("значення 200%", report["markdown"])
            self.assertNotIn(" percent", report["markdown"])
            self.assertIn("\n\npl:", report["markdown"])
            self.assertNotIn("ranking", report["prioritization"])
            page = evidence_page(report)
            self.assertEqual(page["prioritization"]["counts"], report["prioritization"]["counts"])
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_cli_uses_exact_rules_file_and_saves_traceable_recommendations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            output, markdown = root / "report.json", root / "report.md"
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["report", "--study", str(study.path), "--study-sha256", study.sha256,
                             "--question", QUESTION, "--criteria-rules", str(rules.path), rules.sha256,
                             "--output", str(output), "--markdown", str(markdown)])
            self.assertEqual(code, 0, stream.getvalue())
            result = json.loads(stream.getvalue())
            full = json.loads(output.read_text())
            self.assertEqual(full["markdown"], markdown.read_text())
            self.assertEqual(result["prioritization"]["counts"]["matches"], 1)
            self.assertEqual(result["prioritization"]["rules_sha256"], rules.sha256)
            self.assertEqual(result["evidence"][0]["criteria_evaluation"]["checks"][0]["exact_value"],
                             {"numerator": 200, "denominator": 1})

    def test_sensitivity_rules_require_matching_parameters_and_keep_base_change(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, data = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, unmatched=(), split=4)
            analysis, _ = saved_analysis(root, data)
            rules, _ = saved_rules(root, study, [
                rule("change_without_top_days_percent", 100, parameters={"top_days": 1}),
                rule("change_after_trim_percent", 80, identifier="edges", parameters={"trim_days": 1}),
            ])
            report = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules, analysis_artifacts=(analysis,))
            row = report["evidence"][0]
            self.assertEqual(row["analysis"]["comparison"]["change_percent"], 12.5)
            self.assertEqual(row["criteria_evaluation"]["status"], "matches")
            self.assertEqual([check["value"] for check in row["criteria_evaluation"]["checks"]], [100.0, 80.0])
            self.assertTrue(all(check["analysis_sha256"] == analysis.sha256 for check in row["criteria_evaluation"]["checks"]))
            missing = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)
            self.assertEqual(missing["evidence"][0]["criteria_evaluation"]["checks"][0]["reason"], "diagnostics_not_supplied")
            changed, _ = saved_rules(root, study, [rule("change_without_top_days_percent", 100, parameters={"top_days": 3})], name="different.json")
            mismatch = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=changed, analysis_artifacts=(analysis,))
            self.assertEqual(mismatch["evidence"][0]["criteria_evaluation"]["checks"][0]["reason"], "diagnostic_parameters_mismatch")

    def test_all_and_any_preserve_unknowns_without_conflating_them_with_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, None, 10)}, unmatched=())
            for mode, coverage, expected in (("all", 50, "undetermined"), ("all", 100, "does_not_match"),
                                             ("any", 50, "matches"), ("any", 100, "undetermined")):
                with self.subTest(mode=mode, coverage=coverage):
                    rules, _ = saved_rules(root, study, [rule(threshold=0), rule("current_coverage_percent", coverage, identifier="coverage")],
                                           match=mode, name=f"{mode}-{coverage}.json")
                    row = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)["evidence"][0]
                    self.assertEqual(row["criteria_evaluation"]["status"], expected)
                    self.assertEqual(row["criteria_evaluation"]["checks"][0]["status"], "undetermined")
                    self.assertIsNone(row["criteria_evaluation"]["checks"][0]["value"])
                    self.assertIsNone(row["analysis"]["comparison"]["change_percent"])

    def test_partial_means_are_not_full_period_means_and_observed_zero_is_known(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (10, 20, None, 10), "pl": (0, 0, 0, 0), "en": (None,) * 4}, unmatched=())
            rules, _ = saved_rules(root, study, [rule("current_mean_daily_views", 0)])
            report = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)
            checks = [row["criteria_evaluation"]["checks"][0] for row in report["evidence"]]
            self.assertEqual([item["value"] for item in checks], [None, 0.0, None])
            self.assertEqual(checks[0]["reason"], "incomplete_current_coverage")
            self.assertEqual(checks[1]["status"], "matches")
            relative, _ = saved_rules(root, study, name="relative.json")
            rows = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=relative)["evidence"]
            self.assertEqual(rows[1]["criteria_evaluation"]["checks"][0]["reason"], "zero_baseline")
            coverage, _ = saved_rules(root, study, [rule("current_coverage_percent", 0, operator="lte")], name="coverage.json")
            rows = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=coverage)["evidence"]
            self.assertEqual(rows[2]["criteria_evaluation"]["checks"][0]["value"], 0.0)
            self.assertEqual(rows[2]["criteria_evaluation"]["status"], "matches")

    def test_threshold_comparison_uses_exact_ratio_not_rounded_display(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (1, 1, 1, 1, 0, 0)}, split=3, unmatched=())
            for index, (op, threshold, expected) in enumerate((("gt", 0.333333, "matches"), ("lte", 0.333333, "does_not_match"),
                                                              ("lt", 0.333334, "matches"), ("gte", 0.333334, "does_not_match"))):
                with self.subTest(operator=op):
                    rules, _ = saved_rules(root, study, [rule("current_mean_daily_views", threshold, operator=op)], name=f"{index}.json")
                    result = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)
                    check = result["evidence"][0]["criteria_evaluation"]["checks"][0]
                    self.assertEqual(check["value"], 0.333333)
                    self.assertEqual(check["exact_value"], {"numerator": 1, "denominator": 3})
                    self.assertEqual(check["status"], expected)
                    self.assertIn("1/3", result["markdown"])

    def test_json_numeric_precision_loss_is_rejected_not_silently_rounded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root, {"cs": (1, 1, 1, 1)}, unmatched=())
            rules, document = saved_rules(root, study, [rule("current_mean_daily_views", 0)])
            for literal in ("1.00000000000000001", "1e-324"):
                body = json.dumps(document).replace('"threshold": 0', '"threshold": ' + literal).encode()
                rules.path.write_bytes(body)
                reference = JsonArtifact(rules.path, hashlib.sha256(body).hexdigest())
                with self.subTest(literal=literal), self.assertRaises(PageviewsError) as caught:
                    build_report(study.path, study.sha256, question=QUESTION, criteria_rules=reference)
                self.assertEqual(caught.exception.code, "criteria_rules_error")

    def test_invalid_rule_documents_are_structured_failures(self):
        mutations = (
            (("criteria_version",), True), (("study_sha256",), "0" * 64), (("question",), "Different question"),
            (("match",), "weighted_score"), (("rules",), []), (("rules",), [rule()] * 6),
            (("rules",), [rule(), rule()]), (("rules", 0, "metric"), "predicted_demand"),
            (("rules", 0, "operator"), "eval"), (("rules", 0, "operator"), []),
            (("rules", 0, "threshold"), True), (("rules", 0, "threshold"), None),
            (("rules", 0, "threshold"), "150"), (("rules", 0, "threshold"), 10**16),
            (("rules", 0, "description"), "bad\n# injected"), (("rules", 0, "description"), ""),
            (("rules", 0, "id"), ""), (("rules", 0, "parameters"), {"hidden": 1}),
            (("rules", 0, "parameters"), None), (("rules", 0, "metric"), "change_after_trim_percent"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            _, original = saved_rules(root, study)
            for index, (keys, value) in enumerate(mutations):
                with self.subTest(keys=keys, value=value):
                    changed = deepcopy(original)
                    target = changed
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    rules = save_json_artifact(changed, root / f"invalid-{index}.json")
                    with self.assertRaises(PageviewsError) as caught:
                        build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules)
                    self.assertEqual(caught.exception.code, "criteria_rules_error")

    def test_rules_checksum_strict_json_and_scope_protect_against_silent_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            for bad in ("é" * 64, "A" * 64, "0"):
                with self.subTest(bad=bad), self.assertRaises(PageviewsError) as caught:
                    build_report(study.path, study.sha256, question=QUESTION, criteria_rules=JsonArtifact(rules.path, bad))
                self.assertEqual(caught.exception.code, "invalid_request")
            original = rules.path.read_bytes()
            for body in (original + b"\n", b"[]", b"{bad", b'{"match":"all",' + original[1:], b'{"extra":NaN,' + original[1:]):
                rules.path.write_bytes(body)
                checksum = rules.sha256 if body == original + b"\n" else hashlib.sha256(body).hexdigest()
                with self.subTest(body=body[:20]), self.assertRaises(PageviewsError) as caught:
                    build_report(study.path, study.sha256, question=QUESTION, criteria_rules=JsonArtifact(rules.path, checksum))
                self.assertEqual(caught.exception.code, "criteria_rules_error")
            rules.path.write_bytes(original)
            with self.assertRaises(PageviewsError) as caught:
                build_report(study.path, study.sha256, question=QUESTION, criteria_rules=rules, criteria=["Ще один критерій"])
            self.assertEqual(caught.exception.code, "invalid_request")
            with self.assertRaises(PageviewsError):
                build_report(study.path, study.sha256, question="Інше питання", criteria_rules=rules)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Criteria follow-up must reuse pinned study without HTTP"))
    def test_changing_threshold_changes_decision_and_identity_without_changing_data(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            high, _ = saved_rules(root, study)
            low, _ = saved_rules(root, study, [rule(threshold=100)], name="lower.json")
            first = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=high)
            again = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=high)
            changed = build_report(study.path, study.sha256, question=QUESTION, criteria_rules=low)
            self.assertEqual(first, again)
            self.assertNotEqual(first["report_id"], changed["report_id"])
            self.assertEqual(changed["prioritization"]["counts"]["matches"], 2)
            self.assertEqual([row.get("analysis") for row in first["evidence"]], [row.get("analysis") for row in changed["evidence"]])
            pages = [evidence_page(changed, offset=index) for index in range(3)]
            self.assertEqual([p["evidence"][0]["language"] for p in pages], ["cs", "pl", "en"])
            self.assertTrue(all(p["prioritization"]["counts"]["matches"] == 2 for p in pages))
        network.assert_not_called()

    def test_no_rules_preserves_unstructured_criteria_instead_of_guessing(self):
        with TemporaryDirectory() as directory:
            study, _ = saved_study(Path(directory))
            report = build_report(study.path, study.sha256, question=QUESTION, criteria=["Оберіть перспективний ринок"])
            self.assertEqual(report["prioritization"]["status"], "criteria_require_review")
            self.assertFalse(any("criteria_evaluation" in row for row in report["evidence"]))

    def test_rule_errors_never_publish_output_and_stdlib_execution_works(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, _ = saved_study(root)
            rules, _ = saved_rules(root, study)
            args = ["report", "--study", str(study.path), "--study-sha256", study.sha256, "--question", QUESTION,
                    "--criteria-rules", str(rules.path)]
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main([*args, "0" * 64, "--output", str(root / "report.json"), "--markdown", str(root / "report.md")])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stream.getvalue())["error"]["code"], "criteria_rules_error")
            self.assertFalse((root / "report.json").exists())
            self.assertFalse((root / "report.md").exists())
            result = subprocess.run([sys.executable, "-S", "-m", "tools.pageviews", *args, rules.sha256],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""}, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["prioritization"]["status"], "evaluated")

    @unittest.skipUnless(find_spec("matplotlib") is not None, "Optional charts extra is not installed")
    def test_cli_combines_criteria_diagnostics_and_chart_without_losing_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            study, data = saved_study(root, {"cs": (10, 20, 30, 100, 30, 40, 50, 60)}, unmatched=(), split=4)
            analysis, _ = saved_analysis(root, data)
            chart, _ = saved_chart(root, data)
            rules, _ = saved_rules(root, study, [rule("change_without_top_days_percent", 100, parameters={"top_days": 1}),
                                                rule("change_after_trim_percent", 80, identifier="edges", parameters={"trim_days": 1})])
            output = root / "report.json"
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["report", "--study", str(study.path), "--study-sha256", study.sha256, "--question", QUESTION,
                             "--criteria-rules", str(rules.path), rules.sha256,
                             "--analysis-result", str(analysis.path), analysis.sha256,
                             "--chart-result", str(chart.path), chart.sha256, "--output", str(output)])
            self.assertEqual(code, 0, stream.getvalue())
            result = json.loads(stream.getvalue())
            self.assertEqual(result["evidence"][0]["criteria_evaluation"]["status"], "matches")
            self.assertEqual(result["evidence"][0]["additional_evidence"], {"chart": "verified", "diagnostics": "verified"})
            self.assertLessEqual(len(stream.getvalue().encode()), MAX_EVIDENCE_BYTES)