import unittest
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.report_helpers import saved_study
from tools.pageviews.errors import PageviewsError
from tools.pageviews import pdf
from tools.pageviews.pdf import PAGE_HEIGHT, render_report_pdf
from tools.pageviews.reports import build_report


@unittest.skipUnless(find_spec("matplotlib") is not None, "Optional charts extra is not installed")
class ReportPdfTests(unittest.TestCase):
    def test_renders_one_page_pdf_with_its_own_chart_and_embedded_fonts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root / "study", {"cs": (10, 20, 30, 60)}, unmatched=())
            report = build_report(saved.path, question="Чи змінилися перегляди?", summary="Перегляди зросли.")
            output = root / "report.pdf"
            with patch("tools.pageviews.pdf._chart", wraps=pdf._chart) as chart:
                saved_pdf = render_report_pdf(report, output)
            self.assertEqual(saved_pdf, output.resolve())
            self.assertTrue(chart.called)
            content = output.read_bytes()
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertIn(b"/FontFile2", content)
            self.assertIn(b"/Count 1", content)
            self.assertFalse((root / "charts").exists())

    def test_multilingual_report_with_an_unmatched_language_renders(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй", criteria=["зростання"])
            output = root / "report.pdf"
            render_report_pdf(report, output)
            self.assertTrue(output.read_bytes().startswith(b"%PDF-"))

    def test_too_many_languages_are_rejected_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root, {"cs": (10, 20, 30, 60), "pl": (100, 200, 200, 400), "en": (5, 5, 5, 5)}, unmatched=("de",))
            report = build_report(saved.path, question="Порівняй")
            output = root / "report.pdf"
            with patch("tools.pageviews.pdf.MAX_PDF_LANGUAGES", 3), self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, output)
            self.assertEqual(caught.exception.code, "pdf_one_page_not_eligible")
            self.assertFalse(output.exists())

    def test_oversized_content_fails_closed_without_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй")
            output = root / "report.pdf"
            with patch("tools.pageviews.pdf.MARGIN_BOTTOM", PAGE_HEIGHT), \
                 self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, output)
            self.assertEqual(caught.exception.code, "pdf_one_page_not_eligible")
            self.assertFalse(output.exists())

    def test_does_not_overwrite_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй")
            output = root / "existing.pdf"
            output.write_bytes(b"keep me")
            with self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, output)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(output.read_bytes(), b"keep me")

    def test_rejects_non_pdf_extension_and_symlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй")
            with self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, root / "report.txt")
            self.assertEqual(caught.exception.code, "invalid_request")
            target = root / "target.pdf"
            target.write_bytes(b"data")
            link = root / "link.pdf"
            link.symlink_to(target)
            with self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, link)
            self.assertEqual(caught.exception.code, "output_exists")


class ReportPdfMissingDependencyTests(unittest.TestCase):
    def test_missing_matplotlib_is_a_structured_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved, _ = saved_study(root)
            report = build_report(saved.path, question="Порівняй")
            with patch("tools.pageviews.pdf.find_spec", return_value=None), self.assertRaises(PageviewsError) as caught:
                render_report_pdf(report, root / "report.pdf")
            self.assertEqual(caught.exception.code, "missing_dependency")
