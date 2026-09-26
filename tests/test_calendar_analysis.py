import unittest
from datetime import date

from tests.methodology_helpers import calendar_series
from tools.pageviews.analysis import Period, analyze_series
from tools.pageviews.calendar_analysis import compare_calendar_months
from tools.pageviews.errors import PageviewsError


class CalendarComparisonTests(unittest.TestCase):
    def test_leap_february_uses_daily_means_not_monthly_totals(self):
        series = calendar_series(date(2023, 2, 1), date(2024, 2, 29), lambda day, _: 10)
        result = compare_calendar_months(
            series, Period(date(2023, 2, 1), date(2023, 2, 28)),
            Period(date(2024, 2, 1), date(2024, 2, 29)),
        )
        self.assertEqual(result["status"], "complete")
        row = result["pairs"][0]
        self.assertEqual(row["baseline"]["sum_observed_views"], 280)
        self.assertEqual(row["current"]["sum_observed_views"], 290)
        self.assertEqual(row["difference_mean_daily_views"], 0.0)
        self.assertEqual(row["direction"], "equal")
        self.assertEqual(row["change_percent"], 0.0)
        self.assertEqual(result["summary"]["equal_pairs"], 1)
        self.assertFalse(result["seasonality_adjusted"])
        self.assertFalse(result["statistical_inference_performed"])

    def test_partial_missing_and_unmatched_months_remain_visible(self):
        def values(day, _):
            if day == date(2024, 2, 15):
                return None
            return 20 if day.year == 2024 else 10

        series = calendar_series(date(2023, 1, 1), date(2024, 4, 30), values)
        result = compare_calendar_months(
            series, Period(date(2023, 1, 2), date(2023, 3, 31)),
            Period(date(2024, 1, 1), date(2024, 4, 30)),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["reason"] for row in result["pairs"]], [
            "partial_calendar_month", "incomplete_coverage", None, "baseline_month_outside_period",
        ])
        self.assertEqual(result["summary"], {
            "current_months": 4, "computed_pairs": 1, "excluded_pairs": 3,
            "higher_pairs": 1, "lower_pairs": 0, "equal_pairs": 0,
        })
        self.assertEqual(result["pairs"][0]["affected_periods"], ["baseline"])
        self.assertEqual(result["pairs"][1]["affected_periods"], ["current"])
        self.assertIsNone(result["pairs"][3]["baseline"])

    def test_does_not_expand_selected_baseline_to_find_a_previous_year(self):
        series = calendar_series(date(2023, 1, 1), date(2025, 1, 31), lambda day, _: 10)
        result = compare_calendar_months(
            series, Period(date(2023, 1, 1), date(2023, 1, 31)),
            Period(date(2025, 1, 1), date(2025, 1, 31)),
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["pairs"][0]["baseline_month"], "2024-01")
        self.assertEqual(result["pairs"][0]["reason"], "baseline_month_outside_period")
        self.assertEqual(result["baseline_months_without_current_partner"], ["2023-01"])

    def test_zero_baseline_allows_absolute_difference_but_not_percentage(self):
        series = calendar_series(
            date(2024, 1, 1), date(2025, 1, 31), lambda day, _: 5 if day.year == 2025 else 0,
        )
        result = compare_calendar_months(
            series, Period(date(2024, 1, 1), date(2024, 1, 31)),
            Period(date(2025, 1, 1), date(2025, 1, 31)),
        )
        row = result["pairs"][0]
        self.assertEqual(row["difference_mean_daily_views"], 5)
        self.assertEqual(row["direction"], "higher")
        self.assertIsNone(row["change_percent"])
        self.assertEqual(row["change_percent_reason"], "zero_baseline")

    def test_direction_uses_exact_values_even_when_percentage_rounds_to_zero(self):
        series = calendar_series(
            date(2024, 1, 1), date(2025, 1, 31),
            lambda day, _: 10**9 + int(day == date(2025, 1, 1)),
        )
        row = compare_calendar_months(
            series, Period(date(2024, 1, 1), date(2024, 1, 31)),
            Period(date(2025, 1, 1), date(2025, 1, 31)),
        )["pairs"][0]
        self.assertEqual(row["change_percent"], 0.0)
        self.assertEqual(row["direction"], "higher")

    def test_same_period_change_can_describe_a_spike_or_broad_monthly_increases(self):
        baseline = Period(date(2024, 1, 1), date(2024, 12, 31))
        current = Period(date(2025, 1, 1), date(2025, 12, 31))
        broad = calendar_series(baseline.start, current.end, lambda day, _: 100 + int(day.year == 2025))
        spike = calendar_series(
            baseline.start, current.end, lambda day, _: 100 + 365 * int(day == date(2025, 6, 15)),
        )
        for series in (broad, spike):
            self.assertEqual(analyze_series(series, baseline, current)["comparison"]["change_percent"], 1.0)
        broad_result = compare_calendar_months(broad, baseline, current)
        spike_result = compare_calendar_months(spike, baseline, current)
        self.assertEqual(broad_result["summary"]["higher_pairs"], 12)
        self.assertEqual(spike_result["summary"]["higher_pairs"], 1)
        self.assertEqual(spike_result["summary"]["equal_pairs"], 11)
        self.assertNotIn("growth_verdict", broad_result)

    def test_refuses_out_of_snapshot_and_overlapping_periods(self):
        series = calendar_series(date(2024, 1, 1), date(2025, 12, 31), lambda day, _: 10)
        for baseline, current in (
            (Period(date(2023, 1, 1), date(2023, 12, 31)), Period(date(2024, 1, 1), date(2024, 12, 31))),
            (Period(date(2024, 1, 1), date(2024, 12, 31)), Period(date(2024, 12, 31), date(2025, 12, 31))),
        ):
            with self.subTest(baseline=baseline, current=current):
                with self.assertRaises(PageviewsError):
                    compare_calendar_months(series, baseline, current)