import math
import platform
from datetime import date, timedelta
from itertools import groupby
from statistics import NormalDist

from .analysis import Period, summarize_period, validate_period_order
from .errors import PageviewsError
from .models import DailyViews
from .validation import ValidatedSeries

MODEL_NAME = "linear-calendar-hac"
MODEL_VERSION = 1
DAYS_PER_YEAR = 365.25
CONFIDENCE_LEVEL = 0.95
PARAMETER_COUNT = 19


def _statistics_backend():
    try:
        import numpy as np
        import statsmodels
        from statsmodels.regression.linear_model import OLS
    except ImportError as error:
        raise PageviewsError(
            "missing_dependency",
            "Install the statistics extra with uv sync --locked --extra charts --extra statistics; "
            "then use .venv/bin/python. Calendar comparisons need no extra packages.",
        ) from error
    return np, OLS, {
        "numpy": np.__version__, "statsmodels": statsmodels.__version__,
        "python": platform.python_version(),
    }


def require_statistics() -> None:
    _statistics_backend()


def validate_hac_lags(hac_lags: int, window_days: int | None = None) -> None:
    if type(hac_lags) is not int or hac_lags < 0:
        raise PageviewsError("invalid_request", "hac-lags must be an explicit nonnegative integer.")
    if window_days is not None and hac_lags >= window_days:
        raise PageviewsError("invalid_request", "hac-lags must be smaller than the model window's day count.")


def _minimum_end(start: date) -> date | None:
    if start.year > date.max.year - 2:
        return None
    try:
        anniversary = start.replace(year=start.year + 2)
    except ValueError:
        anniversary = date(start.year + 2, 3, 1)
    return anniversary - timedelta(days=1)


def _model_result(window: Period, hac_lags: int) -> dict:
    minimum_end = _minimum_end(window.start)
    return {
        "model": MODEL_NAME, "model_version": MODEL_VERSION,
        "status": "not_computed", "reason": None,
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "unit": "daily_views_per_year", "slope_daily_views_per_year": None,
        "estimand": "conditional_linear_slope_of_daily_mean_over_the_combined_window",
        "period_split_role": "eligibility_only_not_a_two_period_mean_contrast",
        "observations": None, "design_rank": None, "residual_degrees_of_freedom": None,
        "parameters": {
            "response": "raw_daily_views", "time_scale_days_per_year": DAYS_PER_YEAR,
            "time_centering": "mean_elapsed_day", "calendar_controls": ["weekday", "month_of_year"],
            "reference_weekday": "Sunday", "reference_month": "January",
            "parameter_count": PARAMETER_COUNT, "hac_lags": hac_lags,
            "kernel": "bartlett", "small_sample_correction": "n/(n-19)",
            "serial_correlation_adjustment": hac_lags > 0,
        },
        "eligibility_policy": {
            "minimum_calendar_years": 2,
            "minimum_end_inclusive": minimum_end.isoformat() if minimum_end else None,
            "leap_day_anniversary": "March 1 in a non-leap year",
            "is_statistical_sufficiency_guarantee": False,
        },
        "confidence_interval": {
            "kind": "model_conditional_asymptotic", "status": "not_computed",
            "reason": "model_not_fitted", "nominal_level": CONFIDENCE_LEVEL,
            "distribution": "normal", "standard_error": None, "lower": None, "upper": None,
            "multiple_comparisons_adjusted": False,
        },
        "statistical_inference_performed": False, "assumption_review_required": True,
        "assumptions": [
            "A linear conditional mean with stable additive weekday and month effects is appropriate.",
            "Errors have suitable finite moments and weak enough dependence for the HAC approximation.",
            "The window, model and lag were chosen before inspecting inferential results.",
            "Article scope and measurement have no unmodelled structural changes in this window.",
        ],
        "caveat": "This is a conditional slope model of the selected historical window, not the "
        "observed period-change percentage or a forecast. HAC adjusts covariance, not the "
        "spike-sensitive OLS estimate or model misspecification. Calendar controls omit moving "
        "holidays and changing seasonal patterns. Intervals are not simultaneous across languages; "
        "they do not establish persistent growth, unique users or product demand.",
    }


def _design_matrix(days: tuple[DailyViews, ...], np):
    elapsed = np.arange(len(days), dtype=float)
    weekday = np.array([day.day.weekday() for day in days])
    month = np.array([day.day.month for day in days])
    return np.column_stack([
        np.ones(len(days)), (elapsed - elapsed.mean()) / DAYS_PER_YEAR,
        *(weekday == value for value in range(6)),
        *(month == value for value in range(2, 13)),
    ])


def _residual_diagnostics(days, fitted, hac_lags, tolerance, np) -> dict:
    residual = fitted.resid - fitted.resid.mean()
    squared_sum = float(residual @ residual)
    rmse = math.sqrt(squared_sum / len(days))
    lags = sorted({1, 7, 28, 365, hac_lags + 1})
    autocorrelations = [
        {
            "lag_days": lag,
            "value": float(residual[lag:] @ residual[:-lag]) / squared_sum if rmse > tolerance else None,
            "beyond_hac_lags": lag > hac_lags,
        }
        for lag in lags if lag < len(days)
    ]
    months = []
    for month, group in groupby(enumerate(days), key=lambda pair: pair[1].day.isoformat()[:7]):
        indices = [index for index, _ in group]
        months.append({
            "month": month, "days": len(indices),
            "observed_mean": float(np.mean([days[index].views for index in indices])),
            "fitted_mean": float(fitted.fittedvalues[indices].mean()),
            "residual_mean": float(residual[indices].mean()),
        })
    largest = sorted(range(len(days)), key=lambda index: (-abs(residual[index]), index))[:5]
    return {
        "residual_rmse": rmse, "numerical_zero_tolerance_views": tolerance,
        "residual_acf": autocorrelations,
        "acf_definition": "demeaned_lagged_products_divided_by_total_sum_of_squares",
        "monthly_fit": months,
        "largest_absolute_residuals": [
            {"date": days[index].day.isoformat(), "residual": float(residual[index])}
            for index in largest
        ],
        "negative_fitted_days": int(np.count_nonzero(fitted.fittedvalues < -tolerance)),
        "minimum_fitted_views": float(fitted.fittedvalues.min()),
        "interpretation": "Descriptive diagnostics only; no automatic assumption-validity test was passed.",
    }


def _fit_model(days: tuple[DailyViews, ...], hac_lags: int, result: dict, np, ols) -> dict:
    design = _design_matrix(days, np)
    rank = int(np.linalg.matrix_rank(design))
    condition = float(np.linalg.cond(design))
    result.update(
        observations=len(days), design_rank=rank,
        residual_degrees_of_freedom=len(days) - rank, design_condition_number=condition,
    )
    if rank != PARAMETER_COUNT or len(days) <= rank:
        result["reason"] = "rank_deficient_design"
        return result
    if not math.isfinite(condition) or condition > 1 / math.sqrt(np.finfo(float).eps):
        result["reason"] = "ill_conditioned_design"
        return result
    values = np.array([day.views for day in days], dtype=float)
    fitted = ols(values, design, hasconst=True, missing="raise").fit()
    if not all(np.isfinite(array).all() for array in (fitted.params, fitted.resid, fitted.fittedvalues)):
        result["reason"] = "nonfinite_fit"
        return result
    # This detects numerical perfect fits, not a statistical volume cutoff.
    tolerance = 100 * np.finfo(float).eps * max(1.0, float(np.max(np.abs(values))))
    diagnostics = _residual_diagnostics(days, fitted, hac_lags, tolerance, np)
    result.update(
        status="computed", slope_daily_views_per_year=float(fitted.params[1]),
        diagnostics=diagnostics,
        calendar_effects={
            "kind": "fitted_additive_offsets_not_a_seasonality_test",
            "weekday_relative_to_sunday": {
                name: float(fitted.params[2 + index])
                for index, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"))
            },
            "month_relative_to_january": {
                str(month): float(fitted.params[month + 6]) for month in range(2, 13)
            },
        },
    )
    interval = result["confidence_interval"]
    if diagnostics["residual_rmse"] <= tolerance:
        interval["reason"] = "zero_residual_variance"
        return result
    if diagnostics["negative_fitted_days"]:
        interval["reason"] = "negative_fitted_mean"
        return result
    robust = fitted.get_robustcov_results(
        cov_type="HAC", maxlags=hac_lags, kernel="bartlett", use_correction=True, use_t=False,
    )
    covariance = robust.cov_params()
    variance = float(covariance[1, 1])
    if not np.isfinite(covariance).all() or variance <= 0:
        interval["reason"] = "invalid_hac_variance"
        return result
    standard_error = math.sqrt(variance)
    width = NormalDist().inv_cdf((1 + CONFIDENCE_LEVEL) / 2) * standard_error
    slope = result["slope_daily_views_per_year"]
    lower, upper = slope - width, slope + width
    if not all(math.isfinite(value) for value in (lower, upper)) or lower >= upper:
        interval["reason"] = "interval_not_numerically_resolved"
        return result
    interval.update(status="computed", reason=None, standard_error=standard_error, lower=lower, upper=upper)
    result["statistical_inference_performed"] = True
    return result


def fit_calendar_trend(
    series: ValidatedSeries, baseline: Period, current: Period, *, hac_lags: int
) -> dict[str, object]:
    validate_period_order(baseline, current)
    window = Period(baseline.start, current.end)
    validate_hac_lags(hac_lags, window.expected_days)
    summary = summarize_period(series, window)
    result = _model_result(window, hac_lags)
    result["coverage"] = summary.as_dict()["coverage"]
    if (current.start - baseline.end).days != 1:
        result["reason"] = "non_adjacent_periods"
        return result
    minimum_end = _minimum_end(window.start)
    if minimum_end is None or current.end < minimum_end:
        result["reason"] = "insufficient_calendar_span"
        return result
    if summary.missing_days:
        result["reason"] = "incomplete_coverage"
        return result
    days = tuple(day for day in series.days if window.start <= day.day <= window.end)
    if any(day.views > 2**53 for day in days):
        result["reason"] = "counts_exceed_float_precision"
        return result
    np, ols, versions = _statistics_backend()
    result["software"] = versions
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _fit_model(days, hac_lags, result, np, ols)
    except (np.linalg.LinAlgError, ValueError, FloatingPointError, OverflowError) as error:
        result.update(status="not_computed", reason="numerical_failure", slope_daily_views_per_year=None)
        result["confidence_interval"].update(
            status="not_computed", reason="numerical_failure", standard_error=None, lower=None, upper=None,
        )
        result["statistical_inference_performed"] = False
        result["error"] = {"exception_type": type(error).__name__}
        return result