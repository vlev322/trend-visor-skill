from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction
from math import ceil

from .analysis import (
    OUTPUT_DECIMAL_PLACES,
    Period,
    PeriodSummary,
    analyze_series,
    compare_means,
    summarize_period,
    validate_period_order,
)
from .errors import PageviewsError
from .models import DailyViews
from .validation import ValidatedSeries

DIAGNOSTICS_VERSION = 1
TOP_DATE_PREVIEW_LIMIT = 10
DEFAULT_TOP_DAYS = 3
DEFAULT_TRIM_DAYS = 7


def _require_integer(value: int, name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise PageviewsError(
            "invalid_request",
            f"{name} must be an integer greater than or equal to {minimum}.",
        )


def _relative_change(
    baseline_total: int, baseline_days: int, current_total: int, current_days: int
) -> float:
    ratio = Fraction(current_total * baseline_days, baseline_total * current_days)
    return round(float(100 * (ratio - 1)), OUTPUT_DECIMAL_PLACES)


def _break_even_requirements(
    baseline: PeriodSummary, current: PeriodSummary
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "not_computed",
        "reason": None,
        "target": "current_full_period_mean_at_least_baseline",
        "required_missing_views_total": None,
        "required_missing_views_daily_average": None,
    }
    if current.missing_days == 0:
        result["reason"] = "no_current_missing_days"
        return result
    if baseline.status != "complete":
        result["reason"] = "incomplete_baseline"
        return result
    if not baseline.sum_observed_views:
        result["reason"] = "zero_baseline"
        return result

    target_total = ceil(
        Fraction(
            baseline.sum_observed_views * current.expected_days,
            baseline.expected_days,
        )
    )
    required = max(0, target_total - (current.sum_observed_views or 0))
    result["status"] = "computed"
    result["required_missing_views_total"] = required
    result["required_missing_views_daily_average"] = round(
        required / current.missing_days, OUTPUT_DECIMAL_PLACES
    )
    return result


def _conditional_change_bounds(
    baseline: PeriodSummary, current: PeriodSummary, daily_upper_bound: int | None
) -> dict[str, object]:
    has_missing = baseline.missing_days + current.missing_days > 0
    result: dict[str, object] = {
        "kind": "assumption_bounds_not_confidence_interval",
        "status": "not_computed" if has_missing else "not_applicable",
        "reason": None,
        "assumed_daily_upper_bound": daily_upper_bound,
        "unit": "percent",
        "lower_change_percent": None,
        "upper_change_percent": None,
    }
    if not has_missing:
        result["reason"] = "no_missing_days"
        return result
    if daily_upper_bound is None:
        result["reason"] = "missing_upper_bound_required"
        return result

    baseline_low = baseline.sum_observed_views or 0
    current_low = current.sum_observed_views or 0
    if baseline_low == 0:
        result["reason"] = "zero_baseline_possible"
        return result
    baseline_high = baseline_low + baseline.missing_days * daily_upper_bound
    current_high = current_low + current.missing_days * daily_upper_bound
    result["status"] = "computed"
    result["lower_change_percent"] = _relative_change(
        baseline_high, baseline.expected_days, current_low, current.expected_days
    )
    result["upper_change_percent"] = _relative_change(
        baseline_low, baseline.expected_days, current_high, current.expected_days
    )
    return result


def missing_value_sensitivity(
    baseline: PeriodSummary,
    current: PeriodSummary,
    *,
    daily_upper_bound: int | None = None,
) -> dict[str, object]:
    if daily_upper_bound is not None:
        _require_integer(daily_upper_bound, "missing-daily-upper-bound")
    return {
        "missing_days": {
            "baseline": baseline.missing_days,
            "current": current.missing_days,
        },
        "break_even": _break_even_requirements(baseline, current),
        "conditional_bounds": _conditional_change_bounds(
            baseline, current, daily_upper_bound
        ),
        "caveat": "Missing values remain unknown. A break-even requirement is not "
        "an estimate of their actual values. Bounds assume each missing count is "
        "between zero and the supplied cap, with all observed counts held fixed; "
        "they are not confidence intervals or probabilities of growth.",
    }


@dataclass(frozen=True, slots=True)
class _LargestDays:
    original: PeriodSummary
    selected: tuple[DailyViews, ...]

    @property
    def selected_total(self) -> int:
        return sum(day.views or 0 for day in self.selected)

    @property
    def remaining_days(self) -> int:
        return self.original.observed_days - len(self.selected)

    @property
    def remaining_total(self) -> int | None:
        if self.remaining_days == 0 or self.original.sum_observed_views is None:
            return None
        return self.original.sum_observed_views - self.selected_total

    def as_dict(self) -> dict[str, object]:
        total = self.original.sum_observed_views
        remaining = self.remaining_total
        return {
            "window": self.original.as_dict()["window"],
            "observed_days": self.original.observed_days,
            "missing_days": self.original.missing_days,
            "selected_top_days": len(self.selected),
            "top_days": [
                {"date": day.day.isoformat(), "views": day.views}
                for day in self.selected[:TOP_DATE_PREVIEW_LIMIT]
            ],
            "top_dates_truncated": len(self.selected) > TOP_DATE_PREVIEW_LIMIT,
            "share_of_observed_views_percent": (
                round(100 * self.selected_total / total, OUTPUT_DECIMAL_PLACES)
                if total else None
            ),
            "remaining_observed_days": self.remaining_days,
            "sum_observed_views_after_exclusion": remaining,
            "mean_daily_views_after_exclusion": (
                round(remaining / self.remaining_days, OUTPUT_DECIMAL_PLACES)
                if remaining is not None else None
            ),
        }


def _select_largest_days(
    series: ValidatedSeries, period: Period, count: int
) -> _LargestDays:
    summary = summarize_period(series, period)
    observed = [
        day for day in series.days
        if period.start <= day.day <= period.end and day.views is not None
    ]
    ordered = sorted(observed, key=lambda day: (-(day.views or 0), day.day))
    return _LargestDays(original=summary, selected=tuple(ordered[:count]))


def _compare_after_exclusion(
    baseline: _LargestDays, current: _LargestDays
) -> dict[str, object]:
    result: dict[str, object] = {
        "metric": "mean_daily_views_after_largest_days_exclusion",
        "unit": "percent",
        "status": "not_computed",
        "change_percent": None,
        "reason": None,
    }
    if baseline.original.missing_days + current.original.missing_days > 0:
        result["reason"] = "incomplete_coverage"
        return result
    baseline_total = baseline.remaining_total
    current_total = current.remaining_total
    if baseline_total is None or current_total is None:
        result["reason"] = "insufficient_remaining_observations"
        return result
    if baseline_total == 0:
        result["reason"] = "zero_baseline_after_exclusion"
        return result
    result["status"] = "computed"
    result["change_percent"] = _relative_change(
        baseline_total, baseline.remaining_days, current_total, current.remaining_days
    )
    return result


def largest_days_sensitivity(
    series: ValidatedSeries,
    baseline_period: Period,
    current_period: Period,
    *,
    top_days: int = DEFAULT_TOP_DAYS,
) -> dict[str, object]:
    _require_integer(top_days, "top-days", minimum=1)
    validate_period_order(baseline_period, current_period)
    baseline = _select_largest_days(series, baseline_period, top_days)
    current = _select_largest_days(series, current_period, top_days)
    return {
        "method": "exclude_largest_observed_days_from_each_period",
        "requested_top_days_per_period": top_days,
        "baseline": baseline.as_dict(),
        "current": current.as_dict(),
        "original_comparison": compare_means(baseline.original, current.original),
        "comparison_after_exclusion": _compare_after_exclusion(baseline, current),
        "caveat": "Largest days are not automatically errors or bots. The scenario "
        "excludes their counts and days from each mean; it changes the statistic, "
        "does not repair missing data, and is not a corrected trend estimate.",
    }


def window_edge_sensitivity(
    series: ValidatedSeries,
    baseline_period: Period,
    current_period: Period,
    *,
    trim_days: int = DEFAULT_TRIM_DAYS,
) -> dict[str, object]:
    _require_integer(trim_days, "trim-days", minimum=1)
    validate_period_order(baseline_period, current_period)
    baseline = summarize_period(series, baseline_period)
    current = summarize_period(series, current_period)
    too_short = [
        name
        for name, summary in (("baseline", baseline), ("current", current))
        if summary.expected_days <= 2 * trim_days
    ]
    result: dict[str, object] = {
        "method": "trim_fixed_days_from_both_edges_of_each_period",
        "trim_days_per_edge": trim_days,
        "status": "not_computed" if too_short else "computed",
        "reason": "period_too_short" if too_short else None,
        "affected_periods": too_short,
        "original_comparison": compare_means(baseline, current),
        "scenario": None,
        "caveat": "Fixed edge trimming changes the research windows. It is not "
        "seasonal adjustment or a repair of the original missing observations. "
        "Do not select trimming parameters to obtain a preferred outcome.",
    }
    if too_short:
        return result
    offset = timedelta(days=trim_days)
    result["scenario"] = analyze_series(
        series,
        Period(baseline_period.start + offset, baseline_period.end - offset),
        Period(current_period.start + offset, current_period.end - offset),
    )
    return result


def run_diagnostics(
    series: ValidatedSeries,
    baseline_period: Period,
    current_period: Period,
    *,
    top_days: int = DEFAULT_TOP_DAYS,
    trim_days: int = DEFAULT_TRIM_DAYS,
    missing_daily_upper_bound: int | None = None,
) -> dict[str, object]:
    _require_integer(top_days, "top-days", minimum=1)
    _require_integer(trim_days, "trim-days", minimum=1)
    if missing_daily_upper_bound is not None:
        _require_integer(missing_daily_upper_bound, "missing-daily-upper-bound")
    validate_period_order(baseline_period, current_period)
    baseline = summarize_period(series, baseline_period)
    current = summarize_period(series, current_period)
    return {
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "kind": "sensitivity_checks_not_statistical_inference",
        "parameters": {
            "top_days": top_days,
            "trim_days": trim_days,
            "missing_daily_upper_bound": missing_daily_upper_bound,
        },
        "missing_values": missing_value_sensitivity(
            baseline, current, daily_upper_bound=missing_daily_upper_bound
        ),
        "largest_days": largest_days_sensitivity(
            series, baseline_period, current_period, top_days=top_days
        ),
        "window_edges": window_edge_sensitivity(
            series, baseline_period, current_period, trim_days=trim_days
        ),
        "caveat": "These are separate exploratory scenarios, not cumulative "
        "corrections. They do not establish seasonality, statistical confidence, "
        "sustained growth, or product demand; the original analysis is unchanged.",
    }