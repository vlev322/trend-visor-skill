import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.pageviews.artifacts import json_output_path, save_json_artifact
from tools.pageviews.errors import PageviewsError


class JsonArtifactTests(unittest.TestCase):
    def test_saves_unicode_json_with_checksum_and_never_overwrites(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "result.json"
            data = {"title": "Přerušovaný půst", "missing": None, "observed": 0}
            saved = save_json_artifact(data, destination)
            body = destination.read_bytes()
            self.assertEqual(json.loads(body), data)
            self.assertEqual(saved.sha256, hashlib.sha256(body).hexdigest())
            self.assertEqual(saved.path, destination.resolve())
            with self.assertRaises(PageviewsError) as caught:
                save_json_artifact({"replacement": True}, destination)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(destination.read_bytes(), body)

    def test_rejects_symlinks_and_snapshot_descendants(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            for name in ("raw.json", "series.json", "metadata.json"):
                (snapshot / name).write_text("unchanged")
            linked_parent = root / "linked-snapshot"
            linked_parent.symlink_to(snapshot, target_is_directory=True)
            dangling = root / "dangling.json"
            dangling.symlink_to(root / "absent.json")
            for destination, code in (
                (snapshot / "new.json", "invalid_request"),
                (linked_parent / "subdir" / "new.json", "invalid_request"),
                (dangling, "output_exists"),
                (root / "new.txt", "invalid_request"),
            ):
                with self.subTest(destination=destination):
                    with self.assertRaises(PageviewsError) as caught:
                        save_json_artifact({}, destination)
                    self.assertEqual(caught.exception.code, code)
            self.assertFalse((snapshot / "subdir").exists())
            self.assertEqual(len(list(snapshot.iterdir())), 3)
            self.assertTrue(all(path.read_text() == "unchanged" for path in snapshot.iterdir()))

    def test_concurrent_destination_is_not_replaced(self):
        def publish_race(prepared, destination):
            self.assertEqual(json.loads(prepared.read_bytes()), {"ready": True})
            destination.write_bytes(b"earlier writer")
            raise FileExistsError("Destination was created concurrently")

        with TemporaryDirectory() as directory:
            destination = Path(directory) / "result.json"
            with patch("tools.pageviews.artifacts.os.link", side_effect=publish_race):
                with self.assertRaises(PageviewsError) as caught:
                    save_json_artifact({"ready": True}, destination)
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(destination.read_bytes(), b"earlier writer")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_preflight_filesystem_error_is_structured(self):
        with patch.object(Path, "resolve", side_effect=PermissionError("Unreadable path")):
            with self.assertRaises(PageviewsError) as caught:
                json_output_path(Path("/unused/output.json"))
        self.assertEqual(caught.exception.code, "artifact_write_error")

    def test_nonfinite_output_is_rejected_without_creating_files(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "result.json"
            with self.assertRaises(PageviewsError) as caught:
                save_json_artifact({"value": float("nan")}, destination)
            self.assertEqual(caught.exception.code, "invalid_request")
            self.assertEqual(list(Path(directory).iterdir()), [])