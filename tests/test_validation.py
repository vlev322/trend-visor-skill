import unicodedata
import unittest
from urllib.parse import quote

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.errors import PageviewsError
from tools.pageviews.validation import validate_response


class ValidationTests(unittest.TestCase):
    def test_distinguishes_observed_zero_from_missing_day(self):
        body = encode_items(make_item("2026071200", 10), make_item("2026071400", 0))
        series = validate_response(body, make_request())
        self.assertEqual(series.status, "partial")
        self.assertEqual([day.views for day in series.days], [10, None, 0])
        self.assertEqual(series.days[1].as_dict()["status"], "missing")
        self.assertEqual(series.days[2].as_dict()["status"], "observed")
        self.assertEqual(
            series.coverage_summary(),
            {
                "expected_days": 3,
                "observed_days": 2,
                "missing_days": 1,
                "missing_dates": ["2026-07-13"],
                "missing_dates_truncated": False,
                "explicit_zero_days": 1,
            },
        )

    def test_orders_records_and_accepts_all_zero_series(self):
        body = encode_items(
            make_item("2026071400", 0),
            make_item("2026071200", 0),
            make_item("2026071300", 0),
        )
        series = validate_response(body, make_request())
        self.assertEqual(series.status, "complete")
        self.assertEqual(series.coverage_summary()["explicit_zero_days"], 3)
        self.assertEqual(series.days[0].day.isoformat(), "2026-07-12")

    def test_empty_items_means_no_observations_not_zero_views(self):
        series = validate_response(encode_items(), make_request())
        self.assertEqual(series.status, "no_observations")
        self.assertTrue(all(day.views is None for day in series.days))

    def test_limits_missing_date_preview_but_preserves_full_calendar(self):
        request = make_request(start="2026-07-01", end="2026-07-31")
        series = validate_response(encode_items(), request)
        summary = series.coverage_summary()
        self.assertEqual(len(series.days), 31)
        self.assertEqual(summary["missing_days"], 31)
        self.assertEqual(len(summary["missing_dates"]), 10)
        self.assertTrue(summary["missing_dates_truncated"])

    def test_accepts_encoded_titles_and_project_host_suffix(self):
        body = encode_items(
            make_item(
                "2026071200",
                1,
                project="cs.wikipedia.org",
                article="P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst",
            )
        )
        self.assertEqual(validate_response(body, make_request()).days[0].views, 1)

    def test_unicode_normalization_round_trip(self):
        title = unicodedata.normalize("NFD", "Přerušovaný půst")
        request = make_request(article=title)
        body = encode_items(make_item("2026071200", 2, article=quote(title)))
        self.assertEqual(validate_response(body, request).days[0].views, 2)

    def test_percent_sequences_are_not_decoded_twice(self):
        request = make_request(article="100%20 real")
        body = encode_items(make_item("2026071200", 1, article="100%2520_real"))
        self.assertEqual(validate_response(body, request).days[0].views, 1)
        with self.assertRaises(PageviewsError):
            validate_response(
                encode_items(make_item("2026071200", 1, article="100_real")), request
            )

    def test_rejects_invalid_counts(self):
        for views in (-1, 1.5, True, "2", None, float("nan")):
            with self.subTest(views=views):
                self.assert_invalid(encode_items(make_item("2026071200", views)))

    def test_rejects_invalid_timestamps(self):
        for timestamp in ("2026023000", "2026071201", "20260712", 2026071200):
            with self.subTest(timestamp=timestamp):
                self.assert_invalid(encode_items(make_item(timestamp, 1)))

    def test_rejects_other_article_project_agent_access_or_granularity(self):
        for field, value in (
            ("article", "Other_topic"),
            ("project", "pl.wikipedia"),
            ("agent", "all-agents"),
            ("access", "desktop"),
            ("granularity", "monthly"),
        ):
            with self.subTest(field=field):
                item = make_item("2026071200", 1, **{field: value})
                self.assert_invalid(encode_items(item))

    def test_rejects_duplicate_and_out_of_range_dates(self):
        self.assert_invalid(
            encode_items(make_item("2026071200", 1), make_item("2026071200", 1))
        )
        self.assert_invalid(encode_items(make_item("2026071500", 1)))

    def test_rejects_malformed_json_or_schema(self):
        bodies = (b"not json", b"\xff", b"[]", b"{}", b'{"items": null}', encode_items(1))
        for body in bodies:
            with self.subTest(body=body):
                self.assert_invalid(body)

    def assert_invalid(self, body):
        with self.assertRaises(PageviewsError) as caught:
            validate_response(body, make_request())
        self.assertEqual(caught.exception.code, "invalid_response")