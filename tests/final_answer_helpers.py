from copy import deepcopy


def model_fields(answer):
    result = deepcopy(answer)
    for row in result["facts"]:
        row.pop("source_url", None)
        row.pop("chart_path", None)
    conclusions = []
    for row in result["facts"]:
        change = row["change_percent"]
        direction = "not_computed" if change is None else (
            "increase" if change > 0 else ("decrease" if change < 0 else "unchanged")
        )
        conclusions.append({"language": row["language"], "period_change_direction": direction,
                            "trend_interval_available": row["trend_interval_status"] == "computed"})
    return {"facts": result["facts"], "interpretations": conclusions,
            "next_checks": ["review_page_history", "validate_interest_with_users"]}


def final_operations():
    coverage = {"expected_days": 2, "observed_days": 2, "missing_days": 0, "explicit_zero_days": 0}
    analysis = {
        "status": "complete", "method": {"seasonality_adjusted": False},
        "baseline": {"status": "complete", "coverage": deepcopy(coverage), "sum_observed_views": 20,
                     "mean_daily_views_observed": 10.0, "window": {"start": "2026-01-01", "end": "2026-01-02"}},
        "current": {"status": "complete", "coverage": deepcopy(coverage), "sum_observed_views": 40,
                    "mean_daily_views_observed": 20.0, "window": {"start": "2026-01-03", "end": "2026-01-04"}},
        "comparison": {"status": "computed", "change_percent": 100.0, "reason": None},
        "methodology": {
            "calendar_comparison": {"summary": {"computed_pairs": 0}, "seasonality_adjusted": False},
            "trend_model": {
                "status": "computed", "slope_daily_views_per_year": -1.0,
                "parameters": {"calendar_controls": ["weekday", "month_of_year"], "hac_lags": 7},
                "confidence_interval": {"status": "not_computed", "reason": "negative_fitted_mean",
                                        "lower": None, "upper": None},
                "diagnostics": {"negative_fitted_days": 1, "minimum_fitted_views": -2.0},
            },
        },
    }
    row = {
        "language": "uk", "article": "Synthetic article", "project": "uk.wikipedia.org",
        "resolution_status": "matched", "status": "analyzed", "reason": None,
        "analysis": analysis, "source": {"url": "https://wikimedia.org/fixture"},
    }
    return {
        "study_fresh": {"results": [row]},
        "study_offline": {"results": [deepcopy(row)]},
        "analyze_uk": {**deepcopy(analysis), "diagnostics": {"caveat": "Separate scenarios"}},
        "chart_uk": {"artifacts": {"chart": "/tmp/synthetic-chart.png"}},
    }


def final_answer():
    return {
        "facts": [{
            "language": "uk", "resolution_status": "matched", "collection_status": "analyzed",
            "baseline_coverage_status": "complete", "current_coverage_status": "complete",
            "baseline_mean_daily_views_observed": 10.0, "current_mean_daily_views_observed": 20.0,
            "current_missing_days": 0, "change_percent": 100.0, "change_reason": None,
            "source_url": "https://wikimedia.org/fixture", "chart_path": "/tmp/synthetic-chart.png",
            "descriptive_seasonality_adjusted": False,
            "trend_model_status": "computed", "trend_calendar_controls": ["weekday", "month_of_year"],
            "trend_interval_status": "not_computed", "trend_interval_reason": "negative_fitted_mean",
        }],
        "summary_uk": "Описова зміна стосується переглядів однієї статті.",
        "limitations_uk": ["Інтервал не обчислено через від’ємні історичні розрахункові середні."],
        "next_steps_uk": ["Окремо перевірити попит на курс."],
    }