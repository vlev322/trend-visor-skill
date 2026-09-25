import io
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.client import fetch_response
from tools.pageviews.errors import PageviewsError

USER_AGENT = "trend-visor-tests/0.0.1 (offline)"


class ClientTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen")
    def test_sends_one_request_and_preserves_response_bytes(self, opener):
        body = encode_items(make_item("2026071200", 3))
        response = MagicMock(status=200)
        response.read.return_value = body
        opener.return_value.__enter__.return_value = response
        request = make_request()
        raw = fetch_response(request, user_agent=USER_AGENT, timeout=12.0)
        self.assertEqual(raw.body, body)
        self.assertEqual(raw.url, request.url)
        self.assertTrue(raw.fetched_at.endswith("+00:00"))
        opener.assert_called_once()
        sent = opener.call_args.args[0]
        self.assertEqual(sent.full_url, request.url)
        self.assertEqual(sent.get_header("User-agent"), USER_AGENT)
        self.assertEqual(sent.get_header("Accept"), "application/json")
        self.assertEqual(opener.call_args.kwargs["timeout"], 12.0)

    @patch("tools.pageviews.client.urlopen")
    def test_http_errors_are_distinct_and_not_retried(self, opener):
        cases = ((404, "data_unavailable"), (429, "rate_limited"), (503, "http_error"))
        for status, code in cases:
            with self.subTest(status=status):
                opener.reset_mock()
                headers = Message()
                headers["Retry-After"] = "120"
                opener.side_effect = HTTPError(
                    make_request().url, status, "test error", headers, io.BytesIO(b"{}")
                )
                with self.assertRaises(PageviewsError) as caught:
                    fetch_response(make_request(), user_agent=USER_AGENT)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details["retry_after"], "120")
                if status == 404:
                    self.assertIn(
                        "does not establish article absence", str(caught.exception)
                    )
                opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_http_error_without_headers(self, opener):
        opener.side_effect = HTTPError(
            make_request().url, 503, "test error", None, io.BytesIO(b"{}")
        )
        with self.assertRaises(PageviewsError) as caught:
            fetch_response(make_request(), user_agent=USER_AGENT)
        self.assertEqual(caught.exception.code, "http_error")
        self.assertNotIn("retry_after", caught.exception.details)

    @patch("tools.pageviews.client.urlopen")
    def test_network_errors_and_timeouts_do_not_retry(self, opener):
        for error in (URLError("offline"), TimeoutError("test timeout")):
            with self.subTest(error=type(error).__name__):
                opener.reset_mock()
                opener.side_effect = error
                with self.assertRaises(PageviewsError) as caught:
                    fetch_response(make_request(), user_agent=USER_AGENT)
                self.assertEqual(caught.exception.code, "network_error")
                opener.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_invalid_headers_or_timeout_do_not_reach_network(self, opener):
        cases = (
            {"user_agent": ""},
            {"user_agent": "client\nInjected: yes"},
            {"user_agent": USER_AGENT, "timeout": -1},
            {"user_agent": USER_AGENT, "timeout": float("nan")},
        )
        for parameters in cases:
            with self.subTest(parameters=parameters):
                with self.assertRaises(PageviewsError):
                    fetch_response(make_request(), **parameters)
        opener.assert_not_called()

    @patch("tools.pageviews.client.MAX_RESPONSE_BYTES", 3)
    @patch("tools.pageviews.client.urlopen")
    def test_rejects_response_exceeding_size_limit(self, opener):
        response = MagicMock(status=200)
        response.read.return_value = b"1234"
        opener.return_value.__enter__.return_value = response
        with self.assertRaises(PageviewsError) as caught:
            fetch_response(make_request(), user_agent=USER_AGENT)
        self.assertEqual(caught.exception.code, "response_too_large")