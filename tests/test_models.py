import unittest
from datetime import date

from tests.helpers import make_request
from tools.pageviews.errors import PageviewsError


class RequestTests(unittest.TestCase):
    def test_encodes_title_as_one_url_path_segment(self):
        request = make_request(article="A/B & 100%")
        self.assertIn("/A%2FB_%26_100%25/daily/", request.url)
        self.assertTrue(request.url.endswith("/2026071200/2026071400"))
        self.assertIn("/all-access/user/", request.url)

    def test_normalizes_project_and_spaces_without_lowercasing_title(self):
        request = make_request(
            project=" CS.WIKIPEDIA ", article=" Přerušovaný půst "
        )
        self.assertEqual(request.project, "cs.wikipedia.org")
        self.assertEqual(request.article, "Přerušovaný_půst")

    def test_excludes_today_and_seven_completed_days(self):
        request = make_request(start="2024-09-18", end="2026-09-25")
        self.assertEqual(request.end, date(2026, 9, 17))
        self.assertEqual(request.expected_days, 730)
        self.assertTrue(request.as_dict()["end_was_clipped"])
        self.assertEqual(request.requested_end, date(2026, 9, 25))

    def test_zero_lag_still_excludes_current_day(self):
        request = make_request(end="2026-09-25", lag_days=0)
        self.assertEqual(request.end, date(2026, 9, 24))

    def test_counts_leap_day(self):
        request = make_request(start="2024-02-28", end="2024-03-01")
        self.assertEqual(request.expected_days, 3)

    def test_cutoff_crosses_year_boundary(self):
        request = make_request(
            start="2025-12-01", end="2026-01-03", as_of="2026-01-03"
        )
        self.assertEqual(request.end, date(2025, 12, 26))

    def test_extreme_lag_is_reported_as_invalid_input(self):
        with self.assertRaises(PageviewsError) as caught:
            make_request(lag_days=10**18)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_invalid_request_before_network(self):
        cases = (
            {"start": "2026-02-30"},
            {"end": "20260714"},
            {"as_of": "yesterday"},
            {"end": "2026-07-11"},
            {"start": "2015-06-30"},
            {"start": "2026-09-18", "end": "2026-09-25"},
            {"project": "https://cs.wikipedia.org"},
            {"project": "cs.wikipedia.org/other"},
            {"article": ""},
            {"article": "A\nB"},
            {"article": "https://cs.wikipedia.org/wiki/Example"},
            {"lag_days": -1},
            {"lag_days": True},
        )
        for parameters in cases:
            with self.subTest(parameters=parameters):
                with self.assertRaises(PageviewsError) as caught:
                    make_request(**parameters)
                self.assertEqual(caught.exception.code, "invalid_request")