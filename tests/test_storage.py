import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import RawResponse
from tools.pageviews.storage import load_snapshot, read_snapshot, save_snapshot
from tools.pageviews.validation import validate_response


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "snapshots"
        self.request = make_request()
        self.response = RawResponse(
            url=self.request.url,
            fetched_at="2026-09-25T08:00:00+00:00",
            body=encode_items(make_item("2026071200", 5), make_item("2026071400", 0)),
        )
        self.series = validate_response(self.response.body, self.request)

    def save(self):
        return save_snapshot(self.root, self.request, self.response, self.series)

    def write_metadata(self, snapshot, metadata):
        contents = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        (snapshot.directory / "metadata.json").write_bytes(contents)
        pointer_path = snapshot.directory.parent / "latest.json"
        pointer = json.loads(pointer_path.read_bytes())
        pointer["metadata_sha256"] = hashlib.sha256(contents).hexdigest()
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    def test_cache_miss_does_not_create_files(self):
        self.assertIsNone(load_snapshot(self.root, self.request))
        self.assertFalse(self.root.exists())

    def test_saves_raw_bytes_calendar_and_provenance(self):
        snapshot = self.save()
        paths = snapshot.artifact_paths()
        self.assertEqual(Path(paths["raw"]).read_bytes(), self.response.body)
        series = json.loads(Path(paths["series"]).read_bytes())
        self.assertEqual([day["views"] for day in series["days"]], [5, None, 0])
        metadata = json.loads(Path(paths["metadata"]).read_bytes())
        self.assertEqual(metadata["source"]["response_sha256"], self.response.sha256)
        self.assertEqual(metadata["request"], self.request.as_dict())
        self.assertEqual(metadata["coverage"]["missing_dates"], ["2026-07-13"])
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(load_snapshot(self.root, self.request), snapshot)

    def test_new_snapshot_preserves_old_data_and_updates_latest(self):
        original = self.save()
        response = replace(
            self.response,
            fetched_at="2026-09-26T08:00:00+00:00",
            body=encode_items(make_item("2026071200", 6)),
        )
        updated = save_snapshot(
            self.root,
            self.request,
            response,
            validate_response(response.body, self.request),
        )
        self.assertNotEqual(original.directory, updated.directory)
        self.assertEqual(
            (original.directory / "raw.json").read_bytes(), self.response.body
        )
        self.assertEqual(load_snapshot(self.root, self.request), updated)

    def test_reads_an_explicit_historical_snapshot_without_following_latest(self):
        original = self.save()
        original_body = self.response.body
        self.response = replace(
            self.response,
            fetched_at="2026-09-26T08:00:00+00:00",
            body=encode_items(make_item("2026071200", 999)),
        )
        self.series = validate_response(self.response.body, self.request)
        self.save()
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

        loaded = read_snapshot(original.directory)

        self.assertEqual(loaded.directory, original.directory)
        self.assertEqual(loaded.response.body, original_body)
        self.assertEqual(loaded.request, self.request)
        after = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_explicit_snapshot_remains_readable_with_broken_cache_index(self):
        snapshot = self.save()
        (snapshot.directory.parent / "latest.json").write_bytes(b"broken index")

        loaded = read_snapshot(snapshot.directory)

        self.assertEqual(loaded.response.body, self.response.body)
        self.assertEqual(loaded.request, self.request)

    def test_explicit_snapshot_remains_readable_without_cache_index(self):
        snapshot = self.save()
        (snapshot.directory.parent / "latest.json").unlink()
        self.assertEqual(read_snapshot(snapshot.directory), snapshot)

    def test_application_version_change_preserves_compatible_snapshot_and_cache(self):
        snapshot = self.save()
        metadata = json.loads((snapshot.directory / "metadata.json").read_bytes())
        self.assertEqual(metadata["processor_version"], "0.1.0")

        with patch("tools.pageviews.storage.__version__", "0.2.0"):
            self.assertEqual(read_snapshot(snapshot.directory), snapshot)
            self.assertEqual(load_snapshot(self.root, self.request), snapshot)

    def test_invalid_metadata_returns_snapshot_error_after_checksum_verification(self):
        cases = (
            ("request", []),
            ("source", None),
            ("coverage", {}),
            ("schema_version", True),
            ("processor_version", None),
        )
        for field, value in cases:
            with self.subTest(field=field):
                snapshot = self.save()
                metadata = json.loads((snapshot.directory / "metadata.json").read_bytes())
                metadata[field] = value
                self.write_metadata(snapshot, metadata)
                with self.assertRaises(PageviewsError) as caught:
                    read_snapshot(snapshot.directory)
                self.assertEqual(caught.exception.code, "snapshot_error")

    def test_rejects_invalid_stored_request_fields(self):
        for field, value in (
            ("project", None),
            ("excluded_recent_days", True),
            ("as_of", "not a date"),
            ("requested_window", None),
        ):
            with self.subTest(field=field):
                snapshot = self.save()
                metadata = json.loads((snapshot.directory / "metadata.json").read_bytes())
                metadata["request"][field] = value
                self.write_metadata(snapshot, metadata)
                with self.assertRaises(PageviewsError) as caught:
                    read_snapshot(snapshot.directory)
                self.assertEqual(caught.exception.code, "snapshot_error")

    def test_rejects_a_calendar_disagreeing_with_raw_even_with_updated_hashes(self):
        snapshot = self.save()
        series_path = snapshot.directory / "series.json"
        calendar = json.loads(series_path.read_bytes())
        calendar["days"][1] = {
            "date": "2026-07-13", "views": 0, "status": "observed"
        }
        contents = json.dumps(calendar).encode("utf-8")
        series_path.write_bytes(contents)
        metadata = json.loads((snapshot.directory / "metadata.json").read_bytes())
        metadata["series_sha256"] = hashlib.sha256(contents).hexdigest()
        self.write_metadata(snapshot, metadata)

        with self.assertRaises(PageviewsError) as caught:
            read_snapshot(snapshot.directory)

        self.assertEqual(caught.exception.code, "snapshot_error")
        self.assertIn("calendar does not match", caught.exception.details["reason"])

    def test_rejects_a_timezone_naive_fetch_timestamp(self):
        snapshot = self.save()
        metadata = json.loads((snapshot.directory / "metadata.json").read_bytes())
        metadata["source"]["fetched_at_utc"] = "2026-09-25T08:00:00"
        self.write_metadata(snapshot, metadata)
        with self.assertRaises(PageviewsError) as caught:
            read_snapshot(snapshot.directory)
        self.assertEqual(caught.exception.code, "snapshot_error")

    def test_changed_request_does_not_reuse_snapshot(self):
        self.save()
        for parameters in (
            {"article": "Another article"},
            {"project": "pl.wikipedia.org"},
            {"end": "2026-07-15"},
            {"as_of": "2026-09-26"},
            {"lag_days": 0},
        ):
            with self.subTest(parameters=parameters):
                self.assertIsNone(load_snapshot(self.root, make_request(**parameters)))

    def test_corrupted_artifacts_raise_instead_of_silently_refetching(self):
        for name in ("raw.json", "series.json", "metadata.json"):
            with self.subTest(name=name):
                snapshot = self.save()
                (snapshot.directory / name).write_bytes(b"corrupted")
                with self.assertRaises(PageviewsError) as caught:
                    load_snapshot(self.root, self.request)
                self.assertEqual(caught.exception.code, "cache_error")

    def test_missing_snapshot_file_is_not_an_ordinary_cache_miss(self):
        snapshot = self.save()
        (snapshot.directory / "series.json").unlink()
        with self.assertRaises(PageviewsError) as caught:
            load_snapshot(self.root, self.request)
        self.assertEqual(caught.exception.code, "cache_error")

    def test_rejects_invalid_pointer_and_path_traversal(self):
        for pointer in (b"not json", b"[]", b'{"snapshot": "../outside"}'):
            with self.subTest(pointer=pointer):
                snapshot = self.save()
                (snapshot.directory.parent / "latest.json").write_bytes(pointer)
                with self.assertRaises(PageviewsError) as caught:
                    load_snapshot(self.root, self.request)
                self.assertEqual(caught.exception.code, "cache_error")

    def test_failed_publish_leaves_previous_snapshot_accessible(self):
        original = self.save()
        with patch(
            "tools.pageviews.storage._publish_latest",
            side_effect=OSError("test failure"),
        ):
            with self.assertRaises(PageviewsError):
                self.save()
        self.assertEqual(load_snapshot(self.root, self.request), original)

    def test_invalid_destination_and_wrong_response_url_fail_explicitly(self):
        self.root.write_bytes(b"not a directory")
        with self.assertRaises(PageviewsError) as caught:
            self.save()
        self.assertEqual(caught.exception.code, "storage_error")
        with self.assertRaises(PageviewsError):
            save_snapshot(
                self.root,
                self.request,
                replace(self.response, url="https://example.invalid/"),
                self.series,
            )