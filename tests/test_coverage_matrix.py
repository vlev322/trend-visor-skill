import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.report_helpers import saved_study
from tools.pageviews.reports import build_report


class CoverageMatrixTests(unittest.TestCase):
    def test_independent_studies_and_reports_never_merge_rows_or_totals(self):
        """Separate synthetic scopes remain separate; no cross-study aggregate is implied."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_study, _ = saved_study(root / "scope-a", {"cs": (10, 20, 30, 40)}, unmatched=())
            second_study, _ = saved_study(root / "scope-b", {"pl": (100, 200, 300, 400)}, unmatched=())

            first = build_report(first_study.path, first_study.sha256,
                                 question="Окрема синтетична тема A")
            second = build_report(second_study.path, second_study.sha256,
                                  question="Окрема синтетична тема B")

            self.assertNotEqual(first["study_sha256"], second["study_sha256"])
            self.assertNotEqual(first["report_id"], second["report_id"])
            self.assertEqual([row["language"] for row in first["evidence"]], ["cs"])
            self.assertEqual([row["language"] for row in second["evidence"]], ["pl"])
            self.assertEqual(first["evidence"][0]["analysis"]["baseline"]["sum_observed_views"], 30)
            self.assertEqual(second["evidence"][0]["analysis"]["baseline"]["sum_observed_views"], 300)
            self.assertNotIn("combined", first["scope"])
            self.assertNotIn("combined", second["scope"])
