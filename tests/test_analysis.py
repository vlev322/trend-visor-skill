import unittest
from datetime import date, timedelta

from tools.pageviews.analysis import (
    MISSING_DATA_POLICY_ZERO_FILLED,
    Period,
    analyze_series,
    analyze_series_zero_filled,
    months_periods,
    summarize_months,
    summarize_period,
)
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import DailyViews
from tools.pageviews.validation import ValidatedSeries


def make_series(values, start=date(2026, 1, 1)):
    return ValidatedSeries(
        days=tuple(
            DailyViews(start + timedelta(days=offset), value)
            for offset, value in enumerate(values)
        )
    )


class MonthsPeriodsTests(unittest.TestCase):
    def test_twelve_months_compares_against_the_same_months_a_year_earlier(self):
        baseline, current = months_periods(date(2026, 9, 27), 7, 12)
        self.assertEqual(current, Period(date(2025, 9, 1), date(2026, 8, 31)))
        self.assertEqual(baseline, Period(date(2024, 9, 1), date(2025, 8, 31)))

    def test_three_months_still_ends_at_the_last_full_month_before_the_cutoff(self):
        baseline, current = months_periods(date(2026, 9, 27), 7, 3)
        self.assertEqual(current, Period(date(2026, 6, 1), date(2026, 8, 31)))
        self.assertEqual(baseline, Period(date(2025, 6, 1), date(2025, 8, 31)))

    def test_more_than_twelve_months_compares_against_the_immediately_preceding_period(self):
        baseline, current = months_periods(date(2026, 9, 27), 7, 24)
        self.assertEqual(current, Period(date(2024, 9, 1), date(2026, 8, 31)))
        self.assertEqual(baseline, Period(date(2022, 9, 1), date(2024, 8, 31)))

    def test_cutoff_on_a_month_end_uses_that_month_as_the_last_full_month(self):
        _, current = months_periods(date(2026, 9, 8), 7, 1)
        self.assertEqual(current, Period(date(2026, 8, 1), date(2026, 8, 31)))

    def test_rejects_non_positive_months(self):
        with self.assertRaises(PageviewsError):
            months_periods(date(2026, 9, 27), 7, 0)


class PeriodSummaryTests(unittest.TestCase):
    def test_mean_uses_observed_days_including_zero_but_not_missing_days(self):
        series = ValidatedSeries(
            days=(
                DailyViews(date(2026, 7, 12), 10),
                DailyViews(date(2026, 7, 13), None),
                DailyViews(date(2026, 7, 14), 0),
            )
        )
        period = Period(date(2026, 7, 12), date(2026, 7, 14))

        summary = summarize_period(series, period)

        self.assertEqual(summary.status, "partial")
        self.assertEqual(summary.expected_days, 3)
        self.assertEqual(summary.observed_days, 2)
        self.assertEqual(summary.missing_days, 1)
        self.assertEqual(summary.explicit_zero_days, 1)
        self.assertEqual(summary.sum_observed_views, 10)
        self.assertEqual(summary.mean_daily_views_observed, 5.0)
        self.assertIsNone(series.days[1].views)

    def test_no_observations_is_not_an_observed_zero_total(self):
        period = Period(date(2026, 1, 1), date(2026, 1, 2))
        summary = summarize_period(make_series([None, None]), period)
        self.assertEqual(summary.status, "no_observations")
        self.assertEqual(summary.missing_days, 2)
        self.assertIsNone(summary.sum_observed_views)
        self.assertIsNone(summary.mean_daily_views_observed)

    def test_observed_zeros_produce_a_complete_zero_mean(self):
        period = Period(date(2026, 1, 1), date(2026, 1, 2))
        summary = summarize_period(make_series([0, 0]), period)
        self.assertEqual(summary.status, "complete")
        self.assertEqual(summary.explicit_zero_days, 2)
        self.assertEqual(summary.sum_observed_views, 0)
        self.assertEqual(summary.mean_daily_views_observed, 0.0)

    def test_period_selects_only_its_own_inclusive_dates(self):
        series = make_series([999, 5, 7, 999])
        period = Period(date(2026, 1, 2), date(2026, 1, 3))
        summary = summarize_period(series, period)
        self.assertEqual(summary.expected_days, 2)
        self.assertEqual(summary.sum_observed_views, 12)
        self.assertEqual(summary.mean_daily_views_observed, 6.0)

    def test_rejects_reversed_period(self):
        with self.assertRaises(PageviewsError) as caught:
            Period(date(2026, 1, 2), date(2026, 1, 1))
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_periods_outside_snapshot_instead_of_clipping(self):
        series = make_series([1, 2])
        periods = (
            Period(date(2025, 12, 31), date(2026, 1, 1)),
            Period(date(2026, 1, 1), date(2026, 1, 3)),
        )
        for period in periods:
            with self.subTest(period=period):
                with self.assertRaises(PageviewsError) as caught:
                    summarize_period(series, period)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_an_empty_calendar(self):
        period = Period(date(2026, 1, 1), date(2026, 1, 1))
        with self.assertRaises(PageviewsError):
            summarize_period(ValidatedSeries(days=()), period)


class ComparisonTests(unittest.TestCase):
    def test_compares_means_not_totals_for_unequal_period_lengths(self):
        series = make_series([100, 100, 120, 120, 120])
        baseline = Period(date(2026, 1, 1), date(2026, 1, 2))
        current = Period(date(2026, 1, 3), date(2026, 1, 5))

        result = analyze_series(series, baseline, current)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["baseline"]["sum_observed_views"], 200)
        self.assertEqual(result["current"]["sum_observed_views"], 360)
        self.assertEqual(result["comparison"]["metric"], "mean_daily_views")
        self.assertEqual(result["comparison"]["unit"], "percent")
        self.assertEqual(result["comparison"]["status"], "computed")
        self.assertEqual(result["comparison"]["change_percent"], 20.0)
        self.assertIsNone(result["comparison"]["reason"])

    def test_equal_values_decline_and_zero_current_mean(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 1))
        current = Period(date(2026, 1, 2), date(2026, 1, 2))
        for values, expected in (([100, 100], 0), ([100, 50], -50), ([100, 0], -100)):
            with self.subTest(values=values):
                result = analyze_series(make_series(values), baseline, current)
                self.assertEqual(result["comparison"]["change_percent"], expected)

    def test_zero_baseline_is_undefined_even_when_both_periods_are_zero(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 1))
        current = Period(date(2026, 1, 2), date(2026, 1, 2))
        for values in ([0, 100], [0, 0]):
            with self.subTest(values=values):
                result = analyze_series(make_series(values), baseline, current)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["comparison"]["status"], "not_computed")
                self.assertIsNone(result["comparison"]["change_percent"])
                self.assertEqual(result["comparison"]["reason"], "zero_baseline")

    def test_missing_days_block_change_but_keep_observed_summaries(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 2))
        current = Period(date(2026, 1, 3), date(2026, 1, 4))
        cases = (
            ([100, None, 120, 120], ["baseline"]),
            ([100, 100, None, 120], ["current"]),
            ([100, None, None, 120], ["baseline", "current"]),
        )
        for values, affected in cases:
            with self.subTest(values=values):
                result = analyze_series(make_series(values), baseline, current)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["comparison"]["status"], "not_computed")
                self.assertEqual(result["comparison"]["reason"], "incomplete_coverage")
                self.assertEqual(result["comparison"]["affected_periods"], affected)
                self.assertIsNone(result["comparison"]["change_percent"])
                self.assertEqual(result["baseline"]["mean_daily_views_observed"], 100)
                self.assertEqual(result["current"]["mean_daily_views_observed"], 120)

    def test_no_observations_in_either_period_is_explicit(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 1))
        current = Period(date(2026, 1, 2), date(2026, 1, 2))
        result = analyze_series(make_series([None, None]), baseline, current)
        self.assertEqual(result["status"], "no_observations")
        self.assertIsNone(result["comparison"]["change_percent"])

    def test_rejects_overlap_and_reversed_order(self):
        first = Period(date(2026, 1, 1), date(2026, 1, 2))
        later = Period(date(2026, 1, 2), date(2026, 1, 3))
        for baseline, current in ((first, later), (later, first), (first, first)):
            with self.subTest(baseline=baseline, current=current):
                with self.assertRaises(PageviewsError) as caught:
                    analyze_series(make_series([1, 1, 1]), baseline, current)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_ignores_missing_dates_outside_the_two_selected_periods(self):
        baseline = Period(date(2026, 1, 2), date(2026, 1, 2))
        current = Period(date(2026, 1, 4), date(2026, 1, 4))
        series = make_series([None, 100, None, 120, None])
        result = analyze_series(series, baseline, current)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["comparison"]["change_percent"], 20.0)

    def test_comparison_uses_means_before_output_rounding(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 3))
        current = Period(date(2026, 1, 4), date(2026, 1, 5))
        result = analyze_series(make_series([1, 0, 0, 1, 0]), baseline, current)
        self.assertEqual(result["baseline"]["mean_daily_views_observed"], 0.333333)
        self.assertEqual(result["comparison"]["change_percent"], 50.0)

    def test_same_mean_change_does_not_hide_different_monthly_shapes(self):
        baseline = Period(date(2026, 1, 1), date(2026, 3, 1))
        current = Period(date(2026, 3, 2), date(2026, 4, 30))
        gradual = [101 + 2 * (day // 3) for day in range(60)]
        spike = [100] * 59 + [1300]
        gradual_result = analyze_series(
            make_series([100] * 60 + gradual), baseline, current, include_monthly=True
        )
        spike_result = analyze_series(
            make_series([100] * 60 + spike), baseline, current, include_monthly=True
        )
        for result in (gradual_result, spike_result):
            self.assertEqual(result["comparison"]["change_percent"], 20.0)
            self.assertFalse(result["method"]["seasonality_adjusted"])
            self.assertNotIn("verdict", result)
        gradual_months = gradual_result["monthly"]["current"]
        spike_months = spike_result["monthly"]["current"]
        self.assertEqual(
            [row["mean_daily_views_observed"] for row in gradual_months], [110, 130]
        )
        self.assertEqual(
            [row["mean_daily_views_observed"] for row in spike_months], [100, 140]
        )


class MonthlySummaryTests(unittest.TestCase):
    def test_calendar_months_keep_leap_day_and_explicit_partial_boundaries(self):
        series = make_series([5] + [10] * 29 + [15], start=date(2024, 1, 31))
        period = Period(date(2024, 1, 31), date(2024, 3, 1))

        months = summarize_months(series, period)

        self.assertEqual(
            [month["month"] for month in months], ["2024-01", "2024-02", "2024-03"]
        )
        self.assertEqual(
            [month["coverage"]["expected_days"] for month in months], [1, 29, 1]
        )
        self.assertEqual(
            [month["partial_calendar_month"] for month in months], [True, False, True]
        )
        self.assertEqual(
            [month["sum_observed_views"] for month in months], [5, 290, 15]
        )
        self.assertEqual(months[0]["window"]["start"], "2024-01-31")
        self.assertEqual(months[1]["window"]["end"], "2024-02-29")
        self.assertEqual(months[2]["window"]["end"], "2024-03-01")

    def test_full_calendar_month_can_have_no_observations(self):
        series = make_series([None] * 28, start=date(2026, 2, 1))
        period = Period(date(2026, 2, 1), date(2026, 2, 28))
        month = summarize_months(series, period)[0]
        self.assertFalse(month["partial_calendar_month"])
        self.assertEqual(month["status"], "no_observations")
        self.assertEqual(month["coverage"]["missing_days"], 28)
        self.assertIsNone(month["sum_observed_views"])

    def test_monthly_boundaries_cross_year_without_dropping_days(self):
        series = make_series([5, None, 0], start=date(2025, 12, 31))
        period = Period(date(2025, 12, 31), date(2026, 1, 2))
        months = summarize_months(series, period)
        self.assertEqual([month["month"] for month in months], ["2025-12", "2026-01"])
        self.assertEqual(months[0]["coverage"]["expected_days"], 1)
        self.assertEqual(months[1]["coverage"]["expected_days"], 2)
        self.assertEqual(months[1]["coverage"]["missing_days"], 1)
        self.assertEqual(months[1]["coverage"]["explicit_zero_days"], 1)

    def test_monthly_details_are_opt_in(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 1))
        current = Period(date(2026, 1, 2), date(2026, 1, 2))
        result = analyze_series(make_series([100, 120]), baseline, current)
        self.assertNotIn("monthly", result)


class AnalyzeSeriesZeroFilledTests(unittest.TestCase):
    def test_days_the_api_omitted_count_as_zero_instead_of_blocking_the_change(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 2))
        current = Period(date(2026, 1, 3), date(2026, 1, 4))
        series = make_series([10, None, 30, 60])
        result = analyze_series_zero_filled(series, baseline, current)
        self.assertEqual(result["method"]["missing_data_policy"], MISSING_DATA_POLICY_ZERO_FILLED)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["baseline"]["mean_daily_views_observed"], 5.0)
        self.assertEqual(result["baseline"]["coverage"]["missing_days"], 0)
        self.assertEqual(result["baseline"]["coverage"]["assumed_zero_days"], 1)
        self.assertEqual(result["baseline"]["coverage"]["explicit_zero_days"], 0)
        self.assertEqual(result["comparison"]["status"], "computed")
        self.assertEqual(result["comparison"]["change_percent"], 800.0)
        # The original series passed in is not mutated.
        self.assertIsNone(series.days[1].views)

    def test_explicit_zeros_are_not_confused_with_assumed_zeros(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 2))
        current = Period(date(2026, 1, 3), date(2026, 1, 4))
        result = analyze_series_zero_filled(make_series([0, 0, 10, 20]), baseline, current)
        self.assertEqual(result["baseline"]["coverage"]["explicit_zero_days"], 2)
        self.assertEqual(result["baseline"]["coverage"]["assumed_zero_days"], 0)
        self.assertEqual(result["comparison"]["reason"], "zero_baseline")

    def test_a_snapshot_with_no_explicit_rows_anywhere_is_not_silently_filled(self):
        baseline = Period(date(2026, 1, 1), date(2026, 1, 1))
        current = Period(date(2026, 1, 2), date(2026, 1, 2))
        result = analyze_series_zero_filled(make_series([None, None]), baseline, current)
        self.assertEqual(result["status"], "no_observations")
        self.assertIsNone(result["comparison"]["change_percent"])
        self.assertEqual(result["comparison"]["reason"], "incomplete_coverage")
        self.assertNotEqual(result["method"]["missing_data_policy"], MISSING_DATA_POLICY_ZERO_FILLED)