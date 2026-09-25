import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.cli import main
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import RawResponse
from tools.pageviews.validation import validate_response


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "snapshots"
        self.arguments = [
            "--project", "cs.wikipedia.org",
            "--article", "Přerušovaný půst",
            "--start", "2026-07-12",
            "--end", "2026-07-14",
            "--as-of", "2026-09-25",
            "--user-agent", "trend-visor-tests/0.0.1 (offline)",
            "--output-dir", str(self.output),
        ]
        self.response = RawResponse(
            url=make_request().url,
            fetched_at="2026-09-25T08:00:00+00:00",
            body=encode_items(make_item("2026071200", 2), make_item("2026071400", 0)),
        )

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(self.arguments + list(extra))
        return exit_code, json.loads(output.getvalue())

    @patch("tools.pageviews.cli.fetch_response")
    def test_second_identical_request_uses_disk_without_network(self, fetch):
        fetch.return_value = self.response
        first_code, first = self.invoke()
        second_code, second = self.invoke()
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["coverage"]["missing_dates"], ["2026-07-13"])
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(first["source"], second["source"])
        self.assertNotIn("days", second)
        fetch.assert_called_once()

    @patch("tools.pageviews.cli.fetch_response")
    def test_cached_response_is_validated_again(self, fetch):
        fetch.return_value = self.response
        self.invoke()
        with patch(
            "tools.pageviews.cli.validate_response", wraps=validate_response
        ) as validator:
            exit_code, result = self.invoke()
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["cache_hit"])
        validator.assert_called_once()
        self.assertEqual(validator.call_args.args[0], self.response.body)
        fetch.assert_called_once()

    @patch("tools.pageviews.cli.fetch_response")
    def test_refresh_creates_another_snapshot(self, fetch):
        fetch.return_value = self.response
        _, first = self.invoke()
        exit_code, refreshed = self.invoke("--refresh")
        self.assertEqual(exit_code, 0)
        self.assertFalse(refreshed["cache_hit"])
        self.assertNotEqual(first["artifacts"], refreshed["artifacts"])
        self.assertTrue(Path(first["artifacts"]["raw"]).exists())
        self.assertEqual(fetch.call_count, 2)

    @patch("tools.pageviews.cli.fetch_response")
    def test_clipped_window_is_explicit_in_output(self, fetch):
        request = make_request(end="2026-09-25")
        fetch.return_value = replace(self.response, url=request.url)
        exit_code, result = self.invoke("--end", "2026-09-25")
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["request"]["end_was_clipped"])
        self.assertEqual(result["request"]["effective_window"]["end"], "2026-09-17")
        self.assertEqual(result["request"]["requested_window"]["end"], "2026-09-25")

    @patch("tools.pageviews.cli.fetch_response")
    def test_invalid_input_returns_json_and_never_fetches(self, fetch):
        for arguments in (
            ("--start", "2026-02-30"),
            ("--unexpected", "value"),
            ("--timeout", "nan"),
            ("--user-agent", ""),
        ):
            with self.subTest(arguments=arguments):
                exit_code, result = self.invoke(*arguments)
                self.assertEqual(exit_code, 2)
                self.assertEqual(result["status"], "error")
        fetch.assert_not_called()
        self.assertFalse(self.output.exists())

    @patch("tools.pageviews.cli.fetch_response")
    def test_invalid_response_is_not_cached(self, fetch):
        fetch.return_value = replace(self.response, body=b"not json")
        exit_code, result = self.invoke()
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertFalse(self.output.exists())

    @patch("tools.pageviews.cli.fetch_response")
    def test_unavailable_data_does_not_create_zero_series(self, fetch):
        fetch.side_effect = PageviewsError("data_unavailable", "Ambiguous HTTP 404.")
        exit_code, result = self.invoke()
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "data_unavailable")
        self.assertNotIn("coverage", result)
        self.assertFalse(self.output.exists())

    @patch("tools.pageviews.cli.fetch_response")
    def test_corrupt_cache_requires_explicit_refresh(self, fetch):
        fetch.return_value = self.response
        _, first = self.invoke()
        Path(first["artifacts"]["raw"]).write_bytes(b"damaged")
        exit_code, result = self.invoke()
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["error"]["code"], "cache_error")
        fetch.assert_called_once()
        refreshed_code, refreshed = self.invoke("--refresh")
        self.assertEqual(refreshed_code, 0)
        self.assertFalse(refreshed["cache_hit"])
        self.assertEqual(fetch.call_count, 2)

    def test_module_entry_point_displays_help(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "tools.pageviews", "--help"],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--as-of", result.stdout)
        self.assertIn("--refresh", result.stdout)