import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.cli import main
from tools.pageviews.models import RawResponse
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


class ChartCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        request = make_request(end="2026-07-16")
        response = RawResponse(
            url=request.url,
            fetched_at="2026-09-25T08:00:00+00:00",
            body=encode_items(
                make_item("2026071200", 10),
                make_item("2026071400", 0),
                make_item("2026071500", 20),
                make_item("2026071600", 30),
            ),
        )
        self.snapshot = save_snapshot(
            self.root / "cache",
            request,
            response,
            validate_response(response.body, request),
        )
        self.output = self.root / "charts" / "daily.png"
        self.arguments = [
            "chart",
            "--snapshot", str(self.snapshot.directory),
            "--baseline-start", "2026-07-12",
            "--baseline-end", "2026-07-14",
            "--current-start", "2026-07-15",
            "--current-end", "2026-07-16",
            "--output", str(self.output),
        ]

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(self.arguments + list(extra))
        return exit_code, json.loads(output.getvalue())

    @unittest.skipUnless(find_spec("matplotlib"), "Install the charts extra")
    def test_chart_is_created_offline_without_changing_snapshot(self):
        before = {
            path: path.read_bytes()
            for path in (self.root / "cache").rglob("*") if path.is_file()
        }
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No HTTP")
        ) as network:
            exit_code, result = self.invoke()

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["operation"], "chart")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["artifacts"]["chart"], str(self.output.resolve()))
        self.assertTrue(self.output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(result["source"], self.snapshot.response.source_metadata())
        self.assertEqual(result["periods"]["baseline"]["coverage"]["missing_days"], 1)
        self.assertIsNone(result["method"]["smoothing"])
        self.assertNotIn("comparison", result)
        network.assert_not_called()
        after = {
            path: path.read_bytes()
            for path in (self.root / "cache").rglob("*") if path.is_file()
        }
        self.assertEqual(after, before)

    def test_existing_output_is_preserved(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"existing image")
        exit_code, result = self.invoke()
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "output_exists")
        self.assertEqual(self.output.read_bytes(), b"existing image")

    def test_output_inside_snapshot_is_rejected(self):
        for output in (
            self.snapshot.directory / "chart.png",
            self.snapshot.directory / "nested" / "chart.png",
        ):
            with self.subTest(output=output):
                exit_code, result = self.invoke("--output", str(output))
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertFalse(output.exists())
        self.assertFalse((self.snapshot.directory / "nested").exists())

    def test_symlink_into_snapshot_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.snapshot.directory, target_is_directory=True)
        exit_code, result = self.invoke("--output", str(alias / "chart.png"))
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertFalse((self.snapshot.directory / "chart.png").exists())

    def test_other_snapshot_directory_is_also_protected(self):
        request = self.snapshot.request
        other = save_snapshot(
            self.root / "other-cache",
            request,
            self.snapshot.response,
            validate_response(self.snapshot.response.body, request),
        )
        output = other.directory / "chart.png"
        exit_code, result = self.invoke("--output", str(output))
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertFalse(output.exists())

    def test_invalid_periods_and_extension_do_not_write_files(self):
        cases = (
            ("--baseline-start", "2026-02-30"),
            ("--current-start", "2026-07-14"),
            ("--current-end", "2026-07-17"),
            ("--baseline-end", "2026-07-11"),
            ("--output", str(self.root / "bad" / "chart.json")),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                exit_code, result = self.invoke(*arguments)
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "bad").exists())

    def test_missing_and_corrupted_snapshots_are_not_downloaded(self):
        with patch(
            "tools.pageviews.client.urlopen", side_effect=AssertionError("No HTTP")
        ) as network:
            code, result = self.invoke("--snapshot", str(self.root / "absent"))
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "snapshot_error")
            (self.snapshot.directory / "raw.json").write_bytes(b"corrupted")
            code, result = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "snapshot_error")
            network.assert_not_called()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(find_spec("matplotlib"), "Install the charts extra")
    def test_default_outputs_are_unique_but_rendering_is_repeatable(self):
        self.arguments = self.arguments[:-2]
        default = self.root / "default-charts"
        with patch("tools.pageviews.chart_cli.DEFAULT_OUTPUT_DIRECTORY", default):
            first_code, first = self.invoke()
            second_code, second = self.invoke()
        self.assertEqual((first_code, second_code), (0, 0))
        first_path = Path(first["artifacts"]["chart"])
        second_path = Path(second["artifacts"]["chart"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.parent, default)
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        self.assertEqual(first["chart_sha256"], second["chart_sha256"])
        self.assertEqual(
            first["chart_sha256"], hashlib.sha256(first_path.read_bytes()).hexdigest()
        )

    @unittest.skipUnless(find_spec("matplotlib"), "Install the charts extra")
    def test_empty_snapshot_produces_labeled_chart_not_a_zero_series(self):
        request = self.snapshot.request
        response = replace(self.snapshot.response, body=encode_items())
        empty = save_snapshot(
            self.root / "empty", request, response,
            validate_response(response.body, request),
        )
        exit_code, result = self.invoke("--snapshot", str(empty.directory))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "no_observations")
        self.assertEqual(result["periods"]["baseline"]["coverage"]["missing_days"], 3)
        self.assertIsNone(result["periods"]["current"]["sum_observed_views"])
        self.assertTrue(self.output.is_file())

    def run_without_site_packages(self, arguments):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, "-S", "-m", "tools.pageviews", *arguments],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, check=False, timeout=20,
        )

    def test_help_works_without_matplotlib(self):
        result = self.run_without_site_packages(["chart", "--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--snapshot", result.stdout)
        self.assertIn("--output", result.stdout)

    def test_missing_matplotlib_returns_json_with_setup_guidance(self):
        result = self.run_without_site_packages(self.arguments)
        self.assertEqual(result.returncode, 1, result.stderr)
        error = json.loads(result.stdout)["error"]
        self.assertEqual(error["code"], "missing_dependency")
        self.assertIn("uv sync --locked --extra charts", error["message"])
        self.assertFalse(self.output.parent.exists())