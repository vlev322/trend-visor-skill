import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.pageviews.artifacts import save_markdown_artifact
from tools.pageviews.errors import PageviewsError


class MarkdownArtifactTests(unittest.TestCase):
    def test_unicode_report_is_published_with_checksum_and_no_overwrite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.md"
            text = "# Дані\n\nПропуски ≠ нулі.\n"
            saved = save_markdown_artifact(text, path)
            self.assertEqual(path.read_text(), text)
            self.assertEqual(saved.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(PageviewsError) as caught:
                save_markdown_artifact("replace", path)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(path.read_text(), text)

    def test_symlinks_snapshot_outputs_and_bad_text_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("raw.json", "series.json", "metadata.json"):
                (root / name).write_text("unchanged")
            with self.assertRaises(PageviewsError):
                save_markdown_artifact("text", root / "nested" / "report.md")
            self.assertFalse((root / "nested").exists())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "link.md"
            link.symlink_to(root / "absent.md")
            for value, path in (("text", link), ("\ud800", root / "bad.md"), (None, root / "none.md"), ("text", root / "bad.txt")):
                with self.subTest(path=path), self.assertRaises(PageviewsError):
                    save_markdown_artifact(value, path)
            self.assertEqual(list(root.iterdir()), [link])

    def test_size_and_concurrent_publication_do_not_leave_partial_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            with patch("tools.pageviews.artifacts.MAX_ARTIFACT_BYTES", 3), self.assertRaises(PageviewsError):
                save_markdown_artifact("longer", path)
            self.assertFalse(path.exists())

            def race(prepared, destination):
                self.assertEqual(prepared.read_text(), "complete")
                destination.write_text("earlier writer")
                raise FileExistsError("race")

            with patch("tools.pageviews.artifacts.os.link", side_effect=race), self.assertRaises(PageviewsError) as caught:
                save_markdown_artifact("complete", path)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(path.read_text(), "earlier writer")
            self.assertEqual(list(Path(directory).iterdir()), [path])