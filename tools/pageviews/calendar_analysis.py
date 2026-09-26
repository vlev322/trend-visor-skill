from fractions import Fraction

from .analysis import (
    OUTPUT_DECIMAL_PLACES, Period, summarize_months, summarize_period,
    validate_period_order,
)
from .validation import ValidatedSeries


def _month_pair(baseline: dict | None, current: dict, baseline_month: str) -> dict:
    result = {
        "baseline_month": baseline_month, "current_month": current["month"],
        "baseline": baseline, "current": current,
        "status": "not_computed", "reason": None, "affected_periods": [],
        "difference_mean_daily_views": None, "direction": None,
        "change_percent": None, "change_percent_reason": None,
    }
    if baseline is None:
        result.update(reason="baseline_month_outside_period", affected_periods=["baseline"])
        return result
    for field, reason in (
        ("partial_calendar_month", "partial_calendar_month"),
        ("missing_days", "incomplete_coverage"),
    ):
        affected = [
            name for name, row in (("baseline", baseline), ("current", current))
            if (row[field] if field == "partial_calendar_month" else row["coverage"][field])
        ]
        if affected:
            result.update(reason=reason, affected_periods=affected)
            return result
    baseline_mean = Fraction(baseline["sum_observed_views"], baseline["coverage"]["expected_days"])
    current_mean = Fraction(current["sum_observed_views"], current["coverage"]["expected_days"])
    difference = current_mean - baseline_mean
    result.update(
        status="computed", difference_mean_daily_views=round(float(difference), OUTPUT_DECIMAL_PLACES),
        direction="higher" if difference > 0 else ("lower" if difference < 0 else "equal"),
        change_percent=(
            round(float(100 * difference / baseline_mean), OUTPUT_DECIMAL_PLACES)
            if baseline_mean else None
        ),
        change_percent_reason=None if baseline_mean else "zero_baseline",
    )
    return result


def compare_calendar_months(
    series: ValidatedSeries, baseline: Period, current: Period
) -> dict[str, object]:
    validate_period_order(baseline, current)
    for period in (baseline, current):
        summarize_period(series, period)
    baseline_months = {row["month"]: row for row in summarize_months(series, baseline)}
    pairs = []
    for row in summarize_months(series, current):
        year, month = row["month"].split("-")
        previous = f"{int(year) - 1:04d}-{month}"
        pairs.append(_month_pair(baseline_months.get(previous), row, previous))
    computed = [row for row in pairs if row["status"] == "computed"]
    matched_months = {row["baseline_month"] for row in pairs}
    return {
        "method": "same_calendar_month_previous_year",
        "status": "complete" if len(computed) == len(pairs) else (
            "partial" if computed else "unavailable"
        ),
        "seasonality_adjusted": False, "statistical_inference_performed": False,
        "summary": {
            "current_months": len(pairs), "computed_pairs": len(computed),
            "excluded_pairs": len(pairs) - len(computed),
            "higher_pairs": sum(row["direction"] == "higher" for row in computed),
            "lower_pairs": sum(row["direction"] == "lower" for row in computed),
            "equal_pairs": sum(row["direction"] == "equal" for row in computed),
        },
        "baseline_months_without_current_partner": [
            month for month in baseline_months if month not in matched_months
        ],
        "pairs": pairs,
        "caveat": "Only fully observed whole months inside the selected periods are compared. "
        "Leap days are retained and each mean uses its actual day count. Same-month matching "
        "does not control weekday composition, moving holidays or changing seasonal patterns. "
        "Direction counts describe these month pairs, not independent trials or future growth.",
    }