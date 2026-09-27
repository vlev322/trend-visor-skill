import io
import json
import unittest
from datetime import date
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from tests.helpers import make_request
from tests.report_helpers import saved_study
from tools.pageviews.analysis import Period
from tools.pageviews.client import fetch_response
from tools.pageviews.errors import PageviewsError
from tools.pageviews.resolutions import read_resolution
from tools.pageviews.studies import run_study
from tools.pageviews.wikimedia_api import fetch_action
from tools.request_metrics import capture_request_metrics, record_cache_event


class RequestMetricsTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen")
    def test_counts_successful_pageview_and_metadata_http_separately(self, opener):
        pageviews_body = b'{"items":[]}'
        metadata_body = json.dumps({"search": []}).encode()
        responses = []
        for body in (pageviews_body, metadata_body):
            response = MagicMock(status=200)
            response.read.return_value = body
            response.__enter__.return_value = response
            responses.append(response)
        opener.side_effect = responses

        with capture_request_metrics() as metrics:
            fetch_response(make_request(), user_agent="metrics-test/1")
            fetch_action(
                "www.wikidata.org", {"action": "wbsearchentities", "search": "topic"},
                user_agent="metrics-test/1",
            )

        measured = metrics.snapshot()
        self.assertEqual(measured["http"]["pageviews"]["attempts"], 1)
        self.assertEqual(measured["http"]["pageviews"]["successes"], 1)
        self.assertEqual(measured["http"]["pageviews"]["status_codes"], {"200": 1})
        self.assertEqual(measured["http"]["pageviews"]["response_body_bytes_read"], len(pageviews_body))
        self.assertEqual(measured["http"]["metadata"]["attempts"], 1)
        self.assertEqual(measured["http"]["metadata"]["response_body_bytes_read"], len(metadata_body))
        self.assertEqual(measured["http"]["model"]["attempts"], 0)

    @patch("tools.pageviews.client.urlopen")
    def test_http_error_is_counted_as_an_attempt_and_failure(self, opener):
        headers = Message()
        headers["Retry-After"] = "5"
        opener.side_effect = HTTPError(make_request().url, 429, "limited", headers, io.BytesIO(b"{}"))
        with capture_request_metrics() as metrics:
            with self.assertRaises(PageviewsError) as caught:
                fetch_response(make_request(), user_agent="metrics-test/1")
        self.assertEqual(caught.exception.code, "rate_limited")
        pageviews = metrics.snapshot()["http"]["pageviews"]
        self.assertEqual(pageviews["attempts"], 1)
        self.assertEqual(pageviews["errors"], 1)
        self.assertEqual(pageviews["successes"], 0)
        self.assertEqual(pageviews["status_codes"], {"429": 1})

    def test_cache_lookup_hit_miss_and_offline_failures_are_not_http(self):
        with patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network")), TemporaryDirectory() as directory:
            root = Path(directory)
            _, study = saved_study(root)
            resolution = read_resolution(Path(study["resolution"]["path"]), study["resolution"]["sha256"])
            periods = study["periods"]
            baseline = Period(
                date.fromisoformat(periods["baseline"]["start"]),
                date.fromisoformat(periods["baseline"]["end"]),
            )
            current = Period(
                date.fromisoformat(periods["current"]["start"]),
                date.fromisoformat(periods["current"]["end"]),
            )
            with capture_request_metrics() as hit_metrics:
                cached = run_study(
                    resolution, baseline, current, as_of=study["as_of"],
                    cache_dir=root / "cache", offline=True,
                )
            self.assertEqual(cached["summary"]["analyzed_languages"], 2)
            self.assertEqual(hit_metrics.snapshot()["cache"]["hits"], 2)
            self.assertEqual(hit_metrics.snapshot()["http"]["pageviews"]["attempts"], 0)

            with capture_request_metrics() as miss_metrics:
                missing = run_study(
                    resolution, baseline, current, as_of=study["as_of"],
                    cache_dir=root / "empty-cache", offline=True,
                )
            self.assertEqual(missing["summary"]["failed_collections"], 2)
            self.assertEqual(miss_metrics.snapshot()["cache"]["misses"], 2)
            self.assertEqual(miss_metrics.snapshot()["http"]["pageviews"]["attempts"], 0)

    def test_cache_bypasses_are_distinct_from_misses(self):
        with capture_request_metrics() as metrics:
            record_cache_event("bypasses")
        counters = metrics.snapshot()["cache"]
        self.assertEqual(counters["bypasses"], 1)
        self.assertEqual(counters["misses"], 0)

