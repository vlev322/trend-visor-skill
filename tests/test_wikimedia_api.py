import io
import json
import unittest
from email.message import Message
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import RawResponse
from tools.pageviews.wikimedia_api import fetch_action


class ActionApiTests(unittest.TestCase):
    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_encodes_parameters_and_retains_source_metadata(self, fetch):
        body = json.dumps({"search": [], "success": 1}).encode()

        def respond(url, **options):
            return RawResponse(url, "2026-09-25T08:00:00+00:00", body)

        fetch.side_effect = respond
        response = fetch_action(
            "www.wikidata.org",
            {"action": "wbsearchentities", "search": "піст & language=pl", "language": "uk"},
            user_agent="trend-visor-tests/0.0.1 (offline)",
        )

        query = parse_qs(urlsplit(response.source.url).query)
        self.assertEqual(query["search"], ["піст & language=pl"])
        self.assertEqual(query["language"], ["uk"])
        self.assertEqual(query["formatversion"], ["2"])
        self.assertEqual(query["maxlag"], ["5"])
        self.assertEqual(response.payload["search"], [])
        self.assertEqual(response.source.body, body)
        fetch.assert_called_once()

    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_rejects_malformed_json_and_schema(self, fetch):
        for body in (b"not json", b"[]", b"null", b"\xff"):
            with self.subTest(body=body):
                fetch.return_value = RawResponse("https://www.wikidata.org/w/api.php", "now", body)
                with self.assertRaises(PageviewsError) as caught:
                    fetch_action("www.wikidata.org", {"action": "query"}, user_agent="test/1")
                self.assertEqual(caught.exception.code, "invalid_response")

    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_api_errors_inside_successful_http_response_are_not_empty_results(self, fetch):
        for api_code, expected in (
            ("maxlag", "api_busy"), ("ratelimited", "rate_limited"),
            ("badvalue", "api_error"),
        ):
            with self.subTest(api_code=api_code):
                body = json.dumps({"error": {"code": api_code}}).encode()
                fetch.return_value = RawResponse("https://www.wikidata.org/w/api.php", "now", body)
                with self.assertRaises(PageviewsError) as caught:
                    fetch_action("www.wikidata.org", {"action": "query"}, user_agent="test/1")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.details["api_code"], api_code)

    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_warning_about_ignored_parameters_is_not_silenced(self, fetch):
        body = json.dumps({"warnings": {"main": {"warnings": "Unknown parameter"}}}).encode()
        fetch.return_value = RawResponse("https://www.wikidata.org/w/api.php", "now", body)
        with self.assertRaises(PageviewsError) as caught:
            fetch_action("www.wikidata.org", {"action": "query"}, user_agent="test/1")
        self.assertEqual(caught.exception.code, "api_warning")

    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_malformed_error_code_is_a_structured_error(self, fetch):
        for value in ([], {}, 123, None):
            with self.subTest(value=value):
                body = json.dumps({"error": {"code": value}}).encode()
                fetch.return_value = RawResponse(
                    "https://www.wikidata.org/w/api.php", "now", body
                )
                with self.assertRaises(PageviewsError) as caught:
                    fetch_action(
                        "www.wikidata.org", {"action": "query"}, user_agent="test/1"
                    )
                self.assertEqual(caught.exception.code, "invalid_response")

    @patch("tools.pageviews.wikimedia_api.fetch_url")
    def test_refuses_untrusted_hosts_and_write_actions_before_network(self, fetch):
        for host, action in (
            ("localhost", "query"), ("cs.wikipedia.org.evil.example", "query"),
            ("cs.wikipedia.org/path", "query"), ("www.wikidata.org", "edit"),
        ):
            with self.subTest(host=host, action=action):
                with self.assertRaises(PageviewsError):
                    fetch_action(host, {"action": action}, user_agent="test/1")
        fetch.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_action_api_http_errors_do_not_claim_pageview_absence(self, opener):
        for http_status, expected in ((404, "http_error"), (429, "rate_limited")):
            with self.subTest(http_status=http_status):
                opener.reset_mock()
                headers = Message()
                headers["Retry-After"] = "60"
                opener.side_effect = HTTPError(
                    "https://www.wikidata.org/w/api.php", http_status,
                    "test", headers, io.BytesIO(b"{}"),
                )
                with self.assertRaises(PageviewsError) as caught:
                    fetch_action("www.wikidata.org", {"action": "query"}, user_agent="test/1")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.details["retry_after"], "60")
                opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen", side_effect=URLError("offline"))
    def test_network_error_is_explicit(self, opener):
        with self.assertRaises(PageviewsError) as caught:
            fetch_action("www.wikidata.org", {"action": "query"}, user_agent="test/1")
        self.assertEqual(caught.exception.code, "network_error")
        opener.assert_called_once()