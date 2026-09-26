import unittest
from datetime import date, timedelta
from importlib.util import find_spec
from math import sqrt

from tests.methodology_helpers import calendar_series, known_trend_series
from tools.pageviews.analysis import Period
from tools.pageviews.errors import PageviewsError
from tools.pageviews.trend_model import fit_calendar_trend

BASELINE = Period(date(2024, 1, 1), date(2024, 12, 31))
CURRENT = Period(date(2025, 1, 1), date(2025, 12, 31))


class TrendEligibilityTests(unittest.TestCase):
    def test_missing_date_blocks_fit_instead_of_dropping_or_imputing(self):
        series = calendar_series(
            BASELINE.start, CURRENT.end, lambda day, _: None if day == date(2025, 6, 1) else 10,
        )
        result = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
        self.assertEqual(result["status"], "not_computed")
        self.assertEqual(result["reason"], "incomplete_coverage")
        self.assertEqual(result["coverage"]["missing_days"], 1)
        self.assertIsNone(result["slope_daily_views_per_year"])
        self.assertIsNone(result["confidence_interval"]["lower"])

    def test_nonadjacent_periods_never_compress_the_time_axis(self):
        current = Period(date(2025, 1, 2), CURRENT.end)
        result = fit_calendar_trend(known_trend_series(), BASELINE, current, hac_lags=7)
        self.assertEqual(result["reason"], "non_adjacent_periods")

    def test_calendar_anniversary_policy_includes_leap_day_convention(self):
        for start, split, required_end in (
            (date(2024, 1, 1), date(2025, 1, 1), date(2025, 12, 31)),
            (date(2024, 2, 29), date(2025, 3, 1), date(2026, 2, 28)),
        ):
            series = calendar_series(start, required_end, lambda day, _: None)
            baseline = Period(start, split - timedelta(days=1))
            for end, reason in (
                (required_end - timedelta(days=1), "insufficient_calendar_span"),
                (required_end, "incomplete_coverage"),
            ):
                with self.subTest(start=start, end=end):
                    result = fit_calendar_trend(series, baseline, Period(split, end), hac_lags=7)
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(result["eligibility_policy"]["minimum_end_inclusive"], required_end.isoformat())
                    self.assertFalse(result["eligibility_policy"]["is_statistical_sufficiency_guarantee"])

    def test_invalid_lags_are_input_errors_even_when_data_are_incomplete(self):
        series = calendar_series(BASELINE.start, CURRENT.end, lambda day, _: None)
        for lag in (-1, True, 1.5, None, 731, 1000):
            with self.subTest(lag=lag):
                with self.assertRaises(PageviewsError) as caught:
                    fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=lag)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_unrepresentable_integer_counts_do_not_enter_float_model(self):
        series = calendar_series(BASELINE.start, CURRENT.end, lambda day, _: 2**53 + 1)
        result = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
        self.assertEqual(result["reason"], "counts_exceed_float_precision")


@unittest.skipUnless(find_spec("statsmodels") is not None, "Optional statistics extra is not installed")
class TrendModelFitTests(unittest.TestCase):
    def test_recovers_known_slope_separately_from_calendar_effects(self):
        result = fit_calendar_trend(known_trend_series(), BASELINE, CURRENT, hac_lags=7)
        self.assertEqual(result["status"], "computed")
        self.assertAlmostEqual(result["slope_daily_views_per_year"], 365.25, places=8)
        self.assertEqual(result["unit"], "daily_views_per_year")
        self.assertEqual(result["observations"], 731)
        self.assertEqual(result["design_rank"], 19)
        interval = result["confidence_interval"]
        self.assertEqual(interval["status"], "computed")
        self.assertEqual(interval["kind"], "model_conditional_asymptotic")
        self.assertLess(interval["lower"], 365.25)
        self.assertGreater(interval["upper"], 365.25)
        self.assertTrue(result["assumption_review_required"])
        self.assertNotIn("forecast", result)
        self.assertNotIn("growth_verdict", result)

    def test_hac_variance_matches_independent_residualized_time_oracle(self):
        import numpy as np

        series = known_trend_series()
        days = [row.day for row in series.days]
        time = np.arange(len(days), dtype=float) / 365.25
        nuisance = np.column_stack([
            np.ones(len(days)),
            *([day.weekday() == value for day in days] for value in range(1, 7)),
            *([day.month == value for day in days] for value in range(1, 12)),
        ])
        residualized_time = time - nuisance @ np.linalg.lstsq(nuisance, time, rcond=None)[0]
        slope_weights = residualized_time / (residualized_time @ residualized_time)
        residuals = {0: 1, 7: -1, 14: -1, 21: 1}
        for lag in (0, 7, 28):
            with self.subTest(lag=lag):
                variance = len(days) / (len(days) - 19) * sum(
                    slope_weights[i] * ei * slope_weights[j] * ej * (1 - abs(i - j) / (lag + 1))
                    for i, ei in residuals.items() for j, ej in residuals.items() if abs(i - j) <= lag
                )
                result = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=lag)
                interval = result["confidence_interval"]
                self.assertAlmostEqual(interval["standard_error"], sqrt(variance), places=10)
                self.assertAlmostEqual(interval["lower"], 365.25 - 1.959963984540054 * sqrt(variance), places=8)
                self.assertEqual(result["parameters"]["serial_correlation_adjustment"], lag > 0)

    def test_perfect_linear_or_constant_fit_does_not_claim_zero_width_confidence(self):
        for series in (
            known_trend_series(residuals=False),
            calendar_series(BASELINE.start, CURRENT.end, lambda day, _: 10),
            calendar_series(BASELINE.start, CURRENT.end, lambda day, _: 0),
        ):
            with self.subTest(first=series.days[0].views):
                result = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
                self.assertEqual(result["status"], "computed")
                self.assertEqual(result["confidence_interval"]["reason"], "zero_residual_variance")
                self.assertIsNone(result["confidence_interval"]["lower"])
                self.assertFalse(result["statistical_inference_performed"])

    def test_calendar_effects_alone_do_not_create_a_trend(self):
        result = fit_calendar_trend(known_trend_series(slope_per_day=0), BASELINE, CURRENT, hac_lags=7)
        self.assertAlmostEqual(result["slope_daily_views_per_year"], 0, places=9)
        self.assertLess(result["confidence_interval"]["lower"], 0)
        self.assertGreater(result["confidence_interval"]["upper"], 0)
        effects = result["calendar_effects"]
        self.assertAlmostEqual(effects["weekday_relative_to_sunday"]["Monday"], -18, places=9)
        self.assertAlmostEqual(effects["month_relative_to_january"]["12"], 110, places=9)

    def test_changing_only_period_split_does_not_change_combined_window_model(self):
        series = known_trend_series()
        first = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
        second = fit_calendar_trend(
            series, Period(BASELINE.start, date(2024, 6, 30)),
            Period(date(2024, 7, 1), CURRENT.end), hac_lags=7,
        )
        self.assertEqual(first, second)

    def test_scaling_counts_scales_slope_and_standard_error(self):
        series = known_trend_series()
        scaled = calendar_series(BASELINE.start, CURRENT.end, lambda day, offset: 3 * series.days[offset].views + 500)
        original = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
        result = fit_calendar_trend(scaled, BASELINE, CURRENT, hac_lags=7)
        self.assertAlmostEqual(result["slope_daily_views_per_year"], 1095.75, places=8)
        self.assertAlmostEqual(
            result["confidence_interval"]["standard_error"],
            3 * original["confidence_interval"]["standard_error"], places=9,
        )

    def test_sparse_spike_with_negative_fitted_means_withholds_interval(self):
        series = calendar_series(BASELINE.start, CURRENT.end, lambda day, _: 1000 if day == CURRENT.end else 0)
        result = fit_calendar_trend(series, BASELINE, CURRENT, hac_lags=7)
        self.assertEqual(result["status"], "computed")
        self.assertGreater(result["diagnostics"]["negative_fitted_days"], 0)
        self.assertEqual(result["confidence_interval"]["reason"], "negative_fitted_mean")
        self.assertIsNone(result["confidence_interval"]["lower"])

    def test_residual_diagnostics_include_calendar_and_beyond_bandwidth_checks(self):
        result = fit_calendar_trend(known_trend_series(), BASELINE, CURRENT, hac_lags=7)
        diagnostics = result["diagnostics"]
        self.assertEqual(len(diagnostics["monthly_fit"]), 24)
        self.assertAlmostEqual(diagnostics["residual_rmse"], sqrt(4 / 731), places=10)
        self.assertAlmostEqual(diagnostics["monthly_fit"][0]["residual_mean"], 0, places=10)
        acf = {row["lag_days"]: row for row in diagnostics["residual_acf"]}
        self.assertAlmostEqual(acf[7]["value"], -0.25, places=8)
        self.assertTrue(acf[28]["beyond_hac_lags"])
        self.assertFalse(acf[7]["beyond_hac_lags"])