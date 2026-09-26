from dataclasses import dataclass

from .analysis import Period
from .calendar_analysis import compare_calendar_months
from .errors import PageviewsError
from .trend_model import MODEL_NAME, fit_calendar_trend, require_statistics, validate_hac_lags
from .validation import ValidatedSeries

METHODOLOGY_VERSION = 1


@dataclass(frozen=True, slots=True)
class MethodologyOptions:
    trend_model: str | None = None
    hac_lags: int | None = None

    def __post_init__(self) -> None:
        if self.trend_model is not None and self.trend_model != MODEL_NAME:
            raise PageviewsError("invalid_request", f"The supported trend model is {MODEL_NAME}.")
        if self.trend_model is None:
            if self.hac_lags is not None:
                raise PageviewsError("invalid_request", "hac-lags requires an explicit trend-model.")
        else:
            validate_hac_lags(self.hac_lags)

    def as_dict(self) -> dict[str, object]:
        return {"trend_model": self.trend_model, "hac_lags": self.hac_lags}


def prepare_methodology(
    options: MethodologyOptions | None, baseline: Period, current: Period
) -> None:
    if options is not None and options.trend_model is not None:
        validate_hac_lags(options.hac_lags, Period(baseline.start, current.end).expected_days)
        require_statistics()


def assess_methodology(
    series: ValidatedSeries, baseline: Period, current: Period,
    *, options: MethodologyOptions | None = None,
) -> dict[str, object]:
    options = options or MethodologyOptions()
    calendar = compare_calendar_months(series, baseline, current)
    trend = (
        fit_calendar_trend(series, baseline, current, hac_lags=options.hac_lags)
        if options.trend_model is not None else
        {"status": "not_requested", "reason": "no_explicit_trend_model"}
    )
    return {
        "methodology_version": METHODOLOGY_VERSION, "parameters": options.as_dict(),
        "calendar_comparison": calendar, "trend_model": trend,
        "statistical_inference_performed": trend.get("statistical_inference_performed", False),
        "inference_scope": "individual_article_selected_window_only",
        "parent_method_scope": "descriptive_period_summaries_and_mean_comparison",
        "future_growth_estimated": False, "product_demand_estimated": False,
        "interpretation": "Complete observed totals and means describe the saved records; "
        "they do not need a sampling confidence interval. Missing-value uncertainty remains "
        "separate. Month-pair directions describe consistency across the eligible months. "
        "Any trend interval concerns a hypothetical process under the stated model assumptions, "
        "not measurement error, guaranteed persistence or a statistical language ranking.",
    }