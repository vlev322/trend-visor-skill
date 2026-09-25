import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.study_helpers import resolution_result
from tools.pageviews.errors import PageviewsError
from tools.pageviews.resolutions import read_resolution, save_resolution


class ResolutionFileTests(unittest.TestCase):
    def test_read_requires_confirmation_of_exact_saved_bytes(self):
        result = resolution_result(unmatched=("pl",))
        with TemporaryDirectory() as directory:
            saved = save_resolution(result, Path(directory) / "resolution.json")
            original = saved.path.read_bytes()
            plan = read_resolution(saved.path, saved.sha256)

            self.assertEqual(saved.sha256, hashlib.sha256(original).hexdigest())
            self.assertEqual(plan.entity["entity_id"], "Q1666254")
            self.assertEqual([target.language for target in plan.targets], ["cs", "pl"])
            self.assertEqual(plan.targets[0].article, "Czech test article")
            self.assertEqual(plan.targets[1].status, "no_sitelink")
            self.assertIsNone(plan.targets[1].article)
            self.assertEqual(saved.path.read_bytes(), original)

            saved.path.write_bytes(original + b"\n")
            with self.assertRaises(PageviewsError) as caught:
                read_resolution(saved.path, saved.sha256)
            self.assertEqual(caught.exception.code, "confirmation_mismatch")

    def test_rejects_inconsistent_identity_summary_and_provenance(self):
        original = resolution_result(unmatched=("pl",))
        cases = (
            (("schema_version",), True),
            (("resolution_version",), 2),
            (("requires_confirmation",), False),
            (("entity", "entity_id"), "Q1"),
            (("entity", "revision_id"), None),
            (("targets", 0, "download_target", "article"), "Other article"),
            (("targets", 0, "download_target", "project"), "en.wikipedia.org"),
            (("targets", 0, "page", "wikidata_id"), "Q1"),
            (("targets", 0, "page", "page_id"), True),
            (("targets", 0, "page", "namespace"), 1),
            (("targets", 0, "issues"), ["disambiguation"]),
            (("targets", 0, "redirects"), [{"from": "Old", "to": "New"}]),
            (("targets", 0, "sitelink", "badges"), ["Q70893996"]),
            (("targets", 0, "sitelink", "title"), "Article#Section"),
            (("targets", 1, "download_target"), {"article": "Not confirmed"}),
            (("targets", 1, "language"), "cs"),
            (("summary", "requested"), 99),
            (("summary", "matched"), True),
            (("status",), "ready_for_confirmation"),
            (("sources",), None),
            (("sources", "entity", "response_sha256"), "not-a-checksum"),
            (("sources", "entity", "fetched_at_utc"), "2026-09-25T08:00:00"),
            (("sources", "entity", "http_status"), True),
            (("targets", 0, "source", "url"), "https://wrong.wikipedia.org/w/api.php"),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (keys, value) in enumerate(cases):
                with self.subTest(field=keys):
                    result = deepcopy(original)
                    parent = result
                    for key in keys[:-1]:
                        parent = parent[key]
                    parent[keys[-1]] = value
                    body = json.dumps(result).encode()
                    source = root / f"input-{index}.json"
                    source.write_bytes(body)
                    with self.assertRaises(PageviewsError) as caught:
                        read_resolution(source, hashlib.sha256(body).hexdigest())
                    self.assertEqual(caught.exception.code, "resolution_error")
                    with self.assertRaises(PageviewsError) as caught:
                        save_resolution(result, root / f"output-{index}.json")
                    self.assertEqual(caught.exception.code, "resolution_error")

    def test_rejects_ambiguous_or_nonfinite_json(self):
        body = json.dumps(resolution_result()).encode()
        cases = (
            b"not json",
            b"[]",
            b'{"schema_version":2,' + body[1:],
            b'{"extra":NaN,' + body[1:],
            b'{"extra":1e999,' + body[1:],
            b"[" * 1100 + b"]" * 1100,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "resolution.json"
            for value in cases:
                with self.subTest(prefix=value[:30]):
                    path.write_bytes(value)
                    with self.assertRaises(PageviewsError) as caught:
                        read_resolution(path, hashlib.sha256(value).hexdigest())
                    self.assertEqual(caught.exception.code, "resolution_error")

    def test_requires_valid_confirmation_even_when_file_is_missing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            for confirmation in (None, "", "a" * 63, "Z" * 64, "A" * 64):
                with self.subTest(confirmation=confirmation):
                    with self.assertRaises(PageviewsError) as caught:
                        read_resolution(path, confirmation)
                    self.assertEqual(caught.exception.code, "invalid_request")
            with self.assertRaises(PageviewsError) as caught:
                read_resolution(path, "a" * 64)
            self.assertEqual(caught.exception.code, "resolution_error")