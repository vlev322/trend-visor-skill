import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.topic_helpers import entity_payload, matrix_payload, page_payload
from tools.pageviews.cli import main
from tools.pageviews.resolutions import read_resolution


def http_response(payload):
    response = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    response.status = 200
    return response


class TopicCliTests(unittest.TestCase):
    def invoke(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(arguments))
        return code, json.loads(output.getvalue())

    @patch("tools.pageviews.client.urlopen")
    def test_resolve_checks_languages_without_downloading_pageviews(self, opener):
        opener.side_effect = [
            http_response(matrix_payload()),
            http_response(entity_payload()),
            http_response(page_payload()),
        ]
        with patch(
            "tools.pageviews.cli.fetch_response", side_effect=AssertionError("No pageviews")
        ) as pageviews:
            code, result = self.invoke(
                "resolve", "--entity", "Q1666254", "--languages", "pl", "cs",
                "--user-agent", "trend-visor-tests/0.0.1 (offline)",
            )

        self.assertEqual(code, 0)
        self.assertEqual(result["operation"], "resolve")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["targets"][0]["status"], "no_sitelink")
        self.assertEqual(result["targets"][1]["status"], "matched")
        pageviews.assert_not_called()
        self.assertEqual(opener.call_count, 3)
        final_url = urlsplit(opener.call_args.args[0].full_url)
        self.assertEqual(final_url.hostname, "cs.wikipedia.org")
        self.assertEqual(parse_qs(final_url.query)["titles"], ["Přerušovaný půst"])

    @patch("tools.pageviews.client.urlopen")
    def test_resolution_output_preserves_reviewed_mapping(self, opener):
        opener.side_effect = [
            http_response(matrix_payload()), http_response(entity_payload()),
            http_response(page_payload()),
        ]
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "resolution.json"
            code, result = self.invoke(
                "resolve", "--entity", "Q1666254", "--languages", "pl", "cs",
                "--user-agent", "test/1", "--output", str(destination),
            )
            self.assertEqual(code, 0)
            plan = read_resolution(destination)
            self.assertEqual([target.status for target in plan.targets], ["no_sitelink", "matched"])
            self.assertEqual(plan.result["entity"], result["entity"])
            self.assertEqual(plan.result["targets"], result["targets"])
            self.assertNotIn("artifacts", plan.result)

    @patch("tools.pageviews.client.urlopen")
    def test_search_returns_choices_instead_of_resolving_the_first(self, opener):
        opener.return_value = http_response({"success": 1, "search": [
            {"id": "Q1", "label": "First candidate"},
            {"id": "Q2", "label": "Second candidate"},
        ]})
        code, result = self.invoke(
            "search", "--query", "Тема & щось", "--language", "uk",
            "--user-agent", "test/1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["operation"], "search")
        self.assertTrue(result["requires_selection"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("targets", result)
        query = parse_qs(urlsplit(opener.call_args.args[0].full_url).query)
        self.assertEqual(query["search"], ["Тема & щось"])
        opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_invalid_cli_input_does_not_reach_network(self, opener):
        cases = (
            ("search", "--query", ""),
            ("search", "--query", "Topic", "--limit", "0"),
            ("search", "--query", "Topic", "--timeout", "nan"),
            ("resolve", "--entity", "not-an-id", "--languages", "cs"),
            ("resolve", "--entity", "Q1", "--languages", "cs", "CS"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, result = self.invoke(*arguments, "--user-agent", "test/1")
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "error")
        code, result = self.invoke("search", "--query", "Topic")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "invalid_request")
        opener.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_partial_api_failure_returns_nonzero_with_all_targets(self, opener):
        links = {
            "cswiki": {"site": "cswiki", "title": "Czech", "badges": []},
            "plwiki": {"site": "plwiki", "title": "Polish", "badges": []},
        }
        opener.side_effect = [
            http_response(matrix_payload()), http_response(entity_payload(links)),
            http_response({"error": {"code": "maxlag"}}),
        ]
        code, result = self.invoke(
            "resolve", "--entity", "Q1666254", "--languages", "cs", "pl",
            "--user-agent", "test/1",
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["targets"][0]["status"], "check_failed")
        self.assertEqual(result["targets"][0]["error"]["code"], "api_busy")
        self.assertEqual(result["targets"][1]["status"], "not_checked")
        self.assertEqual(opener.call_count, 3)

    @patch("tools.pageviews.client.urlopen")
    def test_resolution_needing_semantic_review_is_not_a_technical_failure(self, opener):
        page = page_payload(identifier="Q1")
        opener.side_effect = [
            http_response(matrix_payload()), http_response(entity_payload()),
            http_response(page),
        ]
        code, result = self.invoke(
            "resolve", "--entity", "Q1666254", "--languages", "cs",
            "--user-agent", "test/1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["targets"][0]["status"], "needs_review")
        self.assertIsNone(result["targets"][0]["download_target"])

    @patch("tools.pageviews.client.urlopen")
    def test_missing_entity_is_reported_without_article_requests(self, opener):
        opener.side_effect = [
            http_response(matrix_payload()),
            http_response({"entities": {"Q1666254": {"missing": True}}}),
        ]
        code, result = self.invoke(
            "resolve", "--entity", "Q1666254", "--languages", "cs",
            "--user-agent", "test/1",
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "entity_unavailable")
        self.assertEqual(opener.call_count, 2)

    def test_help_works_without_optional_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        for operation in ("search", "resolve"):
            with self.subTest(operation=operation):
                result = subprocess.run(
                    [sys.executable, "-S", "-m", "tools.pageviews", operation, "--help"],
                    cwd=root,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--user-agent", result.stdout)
                self.assertNotIn("--snapshot", result.stdout)