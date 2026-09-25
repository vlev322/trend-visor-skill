import unittest
from datetime import date, timedelta

from tools.pageviews.analysis import Period, compare_means, summarize_period
from tools.pageviews.diagnostics import (
    largest_days_sensitivity,
    missing_value_sensitivity,
    window_edge_sensitivity,
)
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import DailyViews
from tools.pageviews.validation import ValidatedSeries


def make_series(values, start=date(2026, 1, 1)):
    return ValidatedSeries(
        days=tuple(
            DailyViews(start + timedelta(days=index), value)
            for index, value in enumerate(values)
        )
    )


def summaries(baseline_values, current_values):
    series = make_series(baseline_values + current_values)
    split = len(baseline_values)
    return (
        summarize_period(series, Period(series.days[0].day, series.days[split - 1].day)),
        summarize_period(series, Period(series.days[split].day, series.days[-1].day)),
    )


class MissingValueTests(unittest.TestCase):
    def test_break_even_is_a_requirement_not_an_imputed_value(self):
        series = make_series([100, 100, 50, None])
        baseline = summarize_period(
            series, Period(date(2026, 1, 1), date(2026, 1, 2))
        )
        current = summarize_period(
            series, Period(date(2026, 1, 3), date(2026, 1, 4))
        )

        result = missing_value_sensitivity(baseline, current)

        threshold = result["break_even"]
        self.assertEqual(threshold["status"], "computed")
        self.assertEqual(threshold["required_missing_views_total"], 150)
        self.assertEqual(threshold["required_missing_views_daily_average"], 150)
        self.assertEqual(
            threshold["target"], "current_full_period_mean_at_least_baseline"
        )
        self.assertEqual(result["conditional_bounds"]["status"], "not_computed")
        self.assertEqual(
            result["conditional_bounds"]["reason"], "missing_upper_bound_required"
        )
        self.assertIsNone(series.days[-1].views)

    def test_conditional_bounds_keep_the_assumption_separate_from_observations(self):
        series = make_series([100, 100, 50, None])
        baseline = summarize_period(
            series, Period(date(2026, 1, 1), date(2026, 1, 2))
        )
        current = summarize_period(
            series, Period(date(2026, 1, 3), date(2026, 1, 4))
        )

        result = missing_value_sensitivity(baseline, current, daily_upper_bound=75)

        bounds = result["conditional_bounds"]
        self.assertEqual(bounds["status"], "computed")
        self.assertEqual(bounds["kind"], "assumption_bounds_not_confidence_interval")
        self.assertEqual(bounds["assumed_daily_upper_bound"], 75)
        self.assertEqual(bounds["lower_change_percent"], -75)
        self.assertEqual(bounds["upper_change_percent"], -37.5)
        self.assertEqual(current.missing_days, 1)
        self.assertEqual(current.mean_daily_views_observed, 50)

    def test_bounds_use_both_periods_and_allow_the_sign_to_vary(self):
        baseline, current = summaries([100, None], [50, None])
        result = missing_value_sensitivity(baseline, current, daily_upper_bound=100)
        bounds = result["conditional_bounds"]
        self.assertEqual(bounds["lower_change_percent"], -75)
        self.assertEqual(bounds["upper_change_percent"], 50)
        self.assertEqual(result["break_even"]["reason"], "incomplete_baseline")
        self.assertIsNone(compare_means(baseline, current)["change_percent"])

    def test_bounds_compare_rates_when_period_lengths_differ(self):
        baseline, current = summaries([100, None, 0], [40, None])
        result = missing_value_sensitivity(baseline, current, daily_upper_bound=20)
        self.assertEqual(result["conditional_bounds"]["lower_change_percent"], -50)
        self.assertEqual(result["conditional_bounds"]["upper_change_percent"], -10)

    def test_break_even_cap_reaches_equal_means_without_claiming_growth(self):
        baseline, current = summaries([100, 100], [50, None])
        result = missing_value_sensitivity(baseline, current, daily_upper_bound=150)
        self.assertEqual(result["conditional_bounds"]["upper_change_percent"], 0)
        self.assertEqual(result["break_even"]["required_missing_views_total"], 150)

    def test_break_even_scales_period_lengths_and_rounds_count_up(self):
        baseline, current = summaries([1, 1, 0], [0, None])
        result = missing_value_sensitivity(baseline, current)
        self.assertEqual(result["break_even"]["required_missing_views_total"], 2)
        self.assertEqual(result["break_even"]["required_missing_views_daily_average"], 2)

    def test_already_reached_baseline_requires_no_additional_missing_counts(self):
        baseline, current = summaries([10], [30, None])
        result = missing_value_sensitivity(baseline, current)
        self.assertEqual(result["break_even"]["required_missing_views_total"], 0)

    def test_all_current_days_missing_still_has_a_break_even_requirement(self):
        baseline, current = summaries([100, 100], [None, None])
        result = missing_value_sensitivity(baseline, current, daily_upper_bound=50)
        self.assertEqual(result["break_even"]["required_missing_views_total"], 200)
        self.assertEqual(result["break_even"]["required_missing_views_daily_average"], 100)
        self.assertEqual(result["conditional_bounds"]["lower_change_percent"], -100)
        self.assertEqual(result["conditional_bounds"]["upper_change_percent"], -50)
        self.assertIsNone(current.sum_observed_views)

    def test_zero_or_missing_baseline_does_not_produce_finite_percentage_bounds(self):
        for baseline_values in ([0, 0], [None, None], [0, None]):
            with self.subTest(baseline=baseline_values):
                baseline, current = summaries(baseline_values, [50, None])
                result = missing_value_sensitivity(
                    baseline, current, daily_upper_bound=100
                )
                bounds = result["conditional_bounds"]
                self.assertEqual(bounds["status"], "not_computed")
                self.assertEqual(bounds["reason"], "zero_baseline_possible")
                self.assertIsNone(bounds["upper_change_percent"])

    def test_complete_data_needs_no_missing_value_scenario(self):
        baseline, current = summaries([100], [120])
        result = missing_value_sensitivity(baseline, current)
        self.assertEqual(result["conditional_bounds"]["status"], "not_applicable")
        self.assertEqual(result["conditional_bounds"]["reason"], "no_missing_days")
        self.assertEqual(result["break_even"]["reason"], "no_current_missing_days")

    def test_zero_cap_is_an_explicit_scenario_not_a_change_to_the_original(self):
        baseline, current = summaries([100], [50, None])
        result = missing_value_sensitivity(baseline, current, daily_upper_bound=0)
        self.assertEqual(result["conditional_bounds"]["lower_change_percent"], -75)
        self.assertEqual(result["conditional_bounds"]["upper_change_percent"], -75)
        self.assertEqual(compare_means(baseline, current)["reason"], "incomplete_coverage")

    def test_increasing_cap_widens_bounds(self):
        baseline, current = summaries([100, None], [50, None])
        small = missing_value_sensitivity(baseline, current, daily_upper_bound=50)
        large = missing_value_sensitivity(baseline, current, daily_upper_bound=100)
        self.assertEqual(small["conditional_bounds"]["upper_change_percent"], 0)
        self.assertLess(
            large["conditional_bounds"]["lower_change_percent"],
            small["conditional_bounds"]["lower_change_percent"],
        )
        self.assertGreater(
            large["conditional_bounds"]["upper_change_percent"],
            small["conditional_bounds"]["upper_change_percent"],
        )

    def test_cap_must_be_a_nonnegative_integer(self):
        baseline, current = summaries([100], [50, None])
        for value in (-1, True, 1.5, "100"):
            with self.subTest(value=value):
                with self.assertRaises(PageviewsError) as caught:
                    missing_value_sensitivity(baseline, current, daily_upper_bound=value)
                self.assertEqual(caught.exception.code, "invalid_request")


class LargestDaysTests(unittest.TestCase):
    def test_short_peak_changes_the_scenario_but_not_the_original_series(self):
        series = make_series([100] * 5 + [100, 100, 100, 100, 200])
        baseline = Period(date(2026, 1, 1), date(2026, 1, 5))
        current = Period(date(2026, 1, 6), date(2026, 1, 10))

        result = largest_days_sensitivity(series, baseline, current, top_days=1)

        self.assertEqual(result["original_comparison"]["change_percent"], 20)
        self.assertEqual(result["comparison_after_exclusion"]["change_percent"], 0)
        self.assertEqual(result["current"]["top_days"], [
            {"date": "2026-01-10", "views": 200}
        ])
        self.assertEqual(result["current"]["share_of_observed_views_percent"], 33.333333)
        self.assertEqual(result["current"]["remaining_observed_days"], 4)
        self.assertEqual(result["current"]["mean_daily_views_after_exclusion"], 100)
        self.assertEqual(series.days[-1].views, 200)

    def test_distributed_level_change_remains_after_exclusion(self):
        series = make_series([100] * 5 + [120] * 5)
        result = largest_days_sensitivity(
            series,
            Period(date(2026, 1, 1), date(2026, 1, 5)),
            Period(date(2026, 1, 6), date(2026, 1, 10)),
            top_days=1,
        )
        self.assertEqual(result["original_comparison"]["change_percent"], 20)
        self.assertEqual(result["comparison_after_exclusion"]["change_percent"], 20)

    def test_exclusion_comparison_uses_remaining_day_counts_for_unequal_windows(self):
        series = make_series([100] * 3 + [120] * 4)
        result = largest_days_sensitivity(
            series,
            Period(series.days[0].day, series.days[2].day),
            Period(series.days[3].day, series.days[-1].day),
            top_days=1,
        )
        self.assertEqual(result["baseline"]["remaining_observed_days"], 2)
        self.assertEqual(result["current"]["remaining_observed_days"], 3)
        self.assertEqual(result["comparison_after_exclusion"]["change_percent"], 20)

    def test_ties_use_earliest_dates_and_selection_excludes_other_windows(self):
        series = make_series([999, 20, 20, 10, 5, 5, 5, 999])
        result = largest_days_sensitivity(
            series,
            Period(date(2026, 1, 2), date(2026, 1, 4)),
            Period(date(2026, 1, 5), date(2026, 1, 7)),
            top_days=1,
        )
        self.assertEqual(result["baseline"]["top_days"], [
            {"date": "2026-01-02", "views": 20}
        ])
        self.assertEqual(result["current"]["top_days"], [
            {"date": "2026-01-05", "views": 5}
        ])

    def test_missing_values_do_not_disappear_from_exclusion_comparison(self):
        series = make_series([10, 10, 10, 10, None, 100])
        result = largest_days_sensitivity(
            series,
            Period(date(2026, 1, 1), date(2026, 1, 3)),
            Period(date(2026, 1, 4), date(2026, 1, 6)),
            top_days=1,
        )
        self.assertEqual(result["current"]["observed_days"], 2)
        self.assertEqual(result["current"]["missing_days"], 1)
        self.assertEqual(result["current"]["remaining_observed_days"], 1)
        self.assertEqual(result["current"]["mean_daily_views_after_exclusion"], 10)
        self.assertEqual(
            result["comparison_after_exclusion"]["reason"], "incomplete_coverage"
        )
        self.assertIsNone(result["comparison_after_exclusion"]["change_percent"])

    def test_too_many_excluded_days_is_explicit(self):
        series = make_series([10, 20, 30, 40])
        result = largest_days_sensitivity(
            series,
            Period(date(2026, 1, 1), date(2026, 1, 2)),
            Period(date(2026, 1, 3), date(2026, 1, 4)),
            top_days=3,
        )
        self.assertEqual(result["requested_top_days_per_period"], 3)
        self.assertEqual(result["baseline"]["selected_top_days"], 2)
        self.assertEqual(result["baseline"]["remaining_observed_days"], 0)
        self.assertIsNone(result["baseline"]["mean_daily_views_after_exclusion"])
        self.assertEqual(
            result["comparison_after_exclusion"]["reason"],
            "insufficient_remaining_observations",
        )

    def test_zero_baseline_after_exclusion_is_not_infinite_growth(self):
        series = make_series([0, 0, 100, 10, 10, 100])
        result = largest_days_sensitivity(
            series,
            Period(date(2026, 1, 1), date(2026, 1, 3)),
            Period(date(2026, 1, 4), date(2026, 1, 6)),
            top_days=1,
        )
        self.assertEqual(result["baseline"]["mean_daily_views_after_exclusion"], 0)
        self.assertEqual(
            result["comparison_after_exclusion"]["reason"], "zero_baseline_after_exclusion"
        )

    def test_all_zero_or_missing_series_does_not_invent_shares(self):
        for values in ([0] * 6, [None] * 6):
            with self.subTest(values=values):
                series = make_series(values)
                result = largest_days_sensitivity(
                    series,
                    Period(date(2026, 1, 1), date(2026, 1, 3)),
                    Period(date(2026, 1, 4), date(2026, 1, 6)),
                    top_days=1,
                )
                self.assertIsNone(result["baseline"]["share_of_observed_views_percent"])
                self.assertIsNone(result["comparison_after_exclusion"]["change_percent"])

    def test_large_selection_has_a_bounded_date_preview(self):
        series = make_series([10] * 60)
        result = largest_days_sensitivity(
            series,
            Period(series.days[0].day, series.days[29].day),
            Period(series.days[30].day, series.days[-1].day),
            top_days=20,
        )
        self.assertEqual(result["baseline"]["selected_top_days"], 20)
        self.assertEqual(len(result["baseline"]["top_days"]), 10)
        self.assertTrue(result["baseline"]["top_dates_truncated"])
        self.assertEqual(result["baseline"]["remaining_observed_days"], 10)

    def test_top_days_must_be_a_positive_integer(self):
        series = make_series([10, 20])
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count):
                with self.assertRaises(PageviewsError):
                    largest_days_sensitivity(
                        series,
                        Period(series.days[0].day, series.days[0].day),
                        Period(series.days[1].day, series.days[1].day),
                        top_days=count,
                    )


class WindowEdgeTests(unittest.TestCase):
    def test_fixed_trimming_preserves_original_comparison_and_exposes_new_windows(self):
        series = make_series(
            [1000, 100, 100, 100, 1000, 2000, 100, 100, 100, 2000]
        )
        baseline = Period(date(2026, 1, 1), date(2026, 1, 5))
        current = Period(date(2026, 1, 6), date(2026, 1, 10))

        result = window_edge_sensitivity(series, baseline, current, trim_days=1)

        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["trim_days_per_edge"], 1)
        self.assertEqual(result["original_comparison"]["change_percent"], 86.956522)
        self.assertEqual(result["scenario"]["comparison"]["change_percent"], 0)
        self.assertEqual(result["scenario"]["baseline"]["window"], {
            "start": "2026-01-02", "end": "2026-01-04"
        })
        self.assertEqual(result["scenario"]["current"]["window"], {
            "start": "2026-01-07", "end": "2026-01-09"
        })

    def test_missing_edge_removal_does_not_make_the_original_complete(self):
        series = make_series([None, 10, 10, 10, None, None, 20, 20, 20, None])
        result = window_edge_sensitivity(
            series,
            Period(series.days[0].day, series.days[4].day),
            Period(series.days[5].day, series.days[-1].day),
            trim_days=1,
        )
        self.assertIsNone(result["original_comparison"]["change_percent"])
        self.assertEqual(result["scenario"]["comparison"]["change_percent"], 100)
        self.assertEqual(result["scenario"]["baseline"]["coverage"]["expected_days"], 3)
        self.assertIsNone(series.days[0].views)

    def test_remaining_missing_days_still_block_scenario_comparison(self):
        series = make_series([10] * 5 + [20, 20, None, 20, 20])
        result = window_edge_sensitivity(
            series,
            Period(series.days[0].day, series.days[4].day),
            Period(series.days[5].day, series.days[-1].day),
            trim_days=1,
        )
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["scenario"]["status"], "partial")
        self.assertEqual(
            result["scenario"]["comparison"]["reason"], "incomplete_coverage"
        )

    def test_short_period_is_not_silently_replaced_with_a_smaller_trim(self):
        series = make_series([10] * 5)
        result = window_edge_sensitivity(
            series,
            Period(series.days[0].day, series.days[1].day),
            Period(series.days[2].day, series.days[-1].day),
            trim_days=1,
        )
        self.assertEqual(result["status"], "not_computed")
        self.assertEqual(result["reason"], "period_too_short")
        self.assertEqual(result["affected_periods"], ["baseline"])
        self.assertIsNone(result["scenario"])

    def test_trim_crosses_months_and_leap_day_correctly(self):
        series = make_series([10] * 8, start=date(2024, 2, 27))
        result = window_edge_sensitivity(
            series,
            Period(series.days[0].day, series.days[3].day),
            Period(series.days[4].day, series.days[-1].day),
            trim_days=1,
        )
        self.assertEqual(result["scenario"]["baseline"]["window"], {
            "start": "2024-02-28", "end": "2024-02-29"
        })
        self.assertEqual(result["scenario"]["comparison"]["change_percent"], 0)

    def test_huge_trim_returns_unavailable_without_date_overflow(self):
        series = make_series([10] * 6)
        result = window_edge_sensitivity(
            series,
            Period(series.days[0].day, series.days[2].day),
            Period(series.days[3].day, series.days[-1].day),
            trim_days=10**18,
        )
        self.assertEqual(result["affected_periods"], ["baseline", "current"])
        self.assertEqual(result["reason"], "period_too_short")

    def test_invalid_trim_or_windows_are_rejected(self):
        series = make_series([10] * 6)
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count):
                with self.assertRaises(PageviewsError):
                    window_edge_sensitivity(
                        series,
                        Period(series.days[0].day, series.days[2].day),
                        Period(series.days[3].day, series.days[-1].day),
                        trim_days=count,
                    )
        with self.assertRaises(PageviewsError):
            window_edge_sensitivity(
                series,
                Period(date(2025, 12, 31), date(2026, 1, 2)),
                Period(date(2026, 1, 3), date(2026, 1, 6)),
            )