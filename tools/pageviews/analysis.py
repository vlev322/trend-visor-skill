from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import PageviewsError
from .validation import ValidatedSeries, fill_missing_as_zero

ANALYSIS_VERSION = 1
MISSING_DATA_POLICY_ZERO_FILLED = "absent_api_rows_counted_as_zero"
OUTPUT_DECIMAL_PLACES = 6


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise PageviewsError(
                "invalid_request", "A comparison period must end on or after its start."
            )

    @property
    def expected_days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True, slots=True)
class PeriodSummary:
    period: Period
    observed_days: int
    explicit_zero_days: int
    sum_observed_views: int | None

    @property
    def expected_days(self) -> int:
        return self.period.expected_days

    @property
    def missing_days(self) -> int:
        return self.expected_days - self.observed_days

    @property
    def status(self) -> str:
        if self.observed_days == 0:
            return "no_observations"
        return "complete" if self.missing_days == 0 else "partial"

    @property
    def mean_daily_views_observed(self) -> float | None:
        if self.sum_observed_views is None or self.observed_days == 0:
            return None
        return self.sum_observed_views / self.observed_days

    def as_dict(self) -> dict[str, object]:
        mean = self.mean_daily_views_observed
        return {
            "window": {
                "start": self.period.start.isoformat(),
                "end": self.period.end.isoformat(),
            },
            "status": self.status,
            "coverage": {
                "expected_days": self.expected_days,
                "observed_days": self.observed_days,
                "missing_days": self.missing_days,
                "explicit_zero_days": self.explicit_zero_days,
            },
            "sum_observed_views": self.sum_observed_views,
            "mean_daily_views_observed": (
                round(mean, OUTPUT_DECIMAL_PLACES) if mean is not None else None
            ),
        }


def summarize_period(series: ValidatedSeries, period: Period) -> PeriodSummary:
    if (
        not series.days
        or period.start < series.days[0].day
        or period.end > series.days[-1].day
    ):
        raise PageviewsError(
            "invalid_request",
            "The entire comparison period must be inside the snapshot's effective window.",
        )
    observed = [
        day.views
        for day in series.days
        if period.start <= day.day <= period.end and day.views is not None
    ]
    return PeriodSummary(
        period=period,
        observed_days=len(observed),
        explicit_zero_days=observed.count(0),
        sum_observed_views=sum(observed) if observed else None,
    )


def summarize_months(
    series: ValidatedSeries, period: Period
) -> list[dict[str, object]]:
    months = []
    start = period.start
    while start <= period.end:
        last_day = monthrange(start.year, start.month)[1]
        calendar_end = date(start.year, start.month, last_day)
        end = min(calendar_end, period.end)
        summary = summarize_period(series, Period(start, end)).as_dict()
        summary["month"] = start.strftime("%Y-%m")
        summary["partial_calendar_month"] = start.day != 1 or end != calendar_end
        months.append(summary)
        if end == period.end:
            break
        start = end + timedelta(days=1)
    return months


def compare_means(
    baseline: PeriodSummary, current: PeriodSummary
) -> dict[str, object]:
    incomplete = [
        name
        for name, summary in (("baseline", baseline), ("current", current))
        if summary.missing_days > 0
    ]
    result: dict[str, object] = {
        "metric": "mean_daily_views",
        "unit": "percent",
        "status": "not_computed",
        "change_percent": None,
        "reason": None,
        "affected_periods": incomplete,
    }
    baseline_mean = baseline.mean_daily_views_observed
    current_mean = current.mean_daily_views_observed
    if incomplete or baseline_mean is None or current_mean is None:
        result["reason"] = "incomplete_coverage"
        return result
    if baseline_mean == 0:
        result["reason"] = "zero_baseline"
        result["affected_periods"] = ["baseline"]
        return result

    change = 100 * (current_mean - baseline_mean) / baseline_mean
    result["status"] = "computed"
    result["change_percent"] = round(change, OUTPUT_DECIMAL_PLACES)
    return result


def validate_period_order(baseline_period: Period, current_period: Period) -> None:
    if baseline_period.end >= current_period.start:
        raise PageviewsError(
            "invalid_request",
            "The baseline must precede the current period without overlapping dates.",
        )


def analyze_series(
    series: ValidatedSeries,
    baseline_period: Period,
    current_period: Period,
    *,
    include_monthly: bool = False,
) -> dict[str, object]:
    validate_period_order(baseline_period, current_period)
    baseline = summarize_period(series, baseline_period)
    current = summarize_period(series, current_period)
    if baseline.observed_days + current.observed_days == 0:
        status = "no_observations"
    elif baseline.missing_days + current.missing_days > 0:
        status = "partial"
    else:
        status = "complete"
    result: dict[str, object] = {
        "analysis_version": ANALYSIS_VERSION,
        "status": status,
        "method": {
            "kind": "descriptive",
            "missing_data_policy": "require_complete_periods_for_change",
            "seasonality_adjusted": False,
            "output_decimal_places": OUTPUT_DECIMAL_PLACES,
        },
        "baseline": baseline.as_dict(),
        "current": current.as_dict(),
        "comparison": compare_means(baseline, current),
    }
    if include_monthly:
        result["monthly"] = {
            "baseline": summarize_months(series, baseline_period),
            "current": summarize_months(series, current_period),
        }
    return result


def analyze_series_zero_filled(
    series: ValidatedSeries,
    baseline_period: Period,
    current_period: Period,
    *,
    include_monthly: bool = False,
) -> dict[str, object]:
    """Same as analyze_series, but days the API omitted (views=None) count as
    an observed 0 instead of blocking the comparison; assumed_zero_days records
    how many of each period's days were filled in this way. If the entire
    snapshot never had a single explicit row, this is too suspicious (e.g. a
    mismatched title) to assume real zero traffic, so it keeps the strict,
    missing-blocks-comparison behavior instead of filling."""
    if series.status == "no_observations":
        return analyze_series(series, baseline_period, current_period, include_monthly=include_monthly)
    original_baseline = summarize_period(series, baseline_period)
    original_current = summarize_period(series, current_period)
    result = analyze_series(
        fill_missing_as_zero(series), baseline_period, current_period,
        include_monthly=include_monthly,
    )
    result["method"]["missing_data_policy"] = MISSING_DATA_POLICY_ZERO_FILLED
    for name, original in (("baseline", original_baseline), ("current", original_current)):
        coverage = result[name]["coverage"]
        coverage["assumed_zero_days"] = original.missing_days
        # Only true API-reported zeros, not the days this wrapper filled in.
        coverage["explicit_zero_days"] = original.explicit_zero_days
    return result