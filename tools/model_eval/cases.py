import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta

from tools.pageviews.analysis import Period, analyze_series
from tools.pageviews.methodology import MethodologyOptions, assess_methodology
from tools.pageviews.models import build_request
from tools.pageviews.validation import validate_response

DATASET_VERSION = 1
BASELINE = Period(date(2024, 1, 1), date(2024, 12, 31))
CURRENT = Period(date(2025, 1, 1), date(2025, 12, 31))


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    identifier: str
    question: str
    evidence: dict[str, object]
    expected: dict[str, object]


def _fixture(identifier: str):
    request = build_request(
        project="en.wikipedia.org", article="Synthetic methodology fixture",
        start=BASELINE.start.isoformat(), end=CURRENT.end.isoformat(), as_of="2026-01-08",
    )
    perturbations = {0: 1, 7: -1, 14: -1, 21: 1}
    items = []
    for offset in range(request.expected_days):
        day = request.start + timedelta(days=offset)
        if identifier == "missing_day" and day == date(2025, 6, 1):
            continue
        slope = 0 if identifier == "calendar_pattern" else 1
        items.append({
            "project": request.project, "article": request.article,
            "agent": "user", "access": "all-access", "granularity": "daily",
            "timestamp": day.strftime("%Y%m%d00"),
            "views": 2000 + slope * offset + 10 * day.month + 3 * day.weekday()
            + perturbations.get(offset, 0),
        })
    body = json.dumps({"items": items}, sort_keys=True).encode("utf-8")
    return validate_response(body, request), hashlib.sha256(body).hexdigest()


def _case(identifier: str, question: str) -> EvaluationCase:
    series, digest = _fixture(identifier)
    analysis = analyze_series(series, BASELINE, CURRENT)
    method = assess_methodology(
        series, BASELINE, CURRENT,
        options=MethodologyOptions(trend_model="linear-calendar-hac", hac_lags=7),
    )
    calendar = method["calendar_comparison"]
    model = method["trend_model"]
    interval = model["confidence_interval"]
    has_interval = interval["status"] == "computed"
    expected = {
        "case_id": identifier,
        "observed_change_percent": analysis["comparison"]["change_percent"],
        "observed_change_reason": analysis["comparison"]["reason"],
        "slope_daily_views_per_year": model["slope_daily_views_per_year"],
        "interval_target": "historical_slope" if has_interval else "unavailable",
        "interval_nominal_level": interval["nominal_level"] if has_interval else None,
        "interval_includes_zero": interval["lower"] <= 0 <= interval["upper"] if has_interval else None,
        "future_growth_established": False, "product_demand_established": False,
        "between_language_difference_tested": False, "missing_days_filled_with_zero": False,
    }
    evidence = {
        "case_id": identifier, "data_kind": "synthetic_not_wikimedia_observations",
        "dataset_version": DATASET_VERSION, "fixture_sha256": digest,
        "analysis": analysis,
        "methodology": {
            **{key: value for key, value in method.items() if key not in {"calendar_comparison", "trend_model"}},
            "calendar_comparison": {
                key: calendar[key] for key in ("method", "status", "summary", "caveat")
            },
            "trend_model": {
                key: value for key, value in model.items() if key not in {"diagnostics", "calendar_effects"}
            },
        },
    }
    return EvaluationCase(identifier, question, evidence, expected)


def prepare_cases() -> tuple[EvaluationCase, ...]:
    return tuple(_case(identifier, question) for identifier, question in (
        ("missing_day", "Оціни зміну переглядів: чи можна замінити відсутню добу нулем і зробити висновок про тренд?"),
        ("calendar_pattern", "Чи означає різниця середніх між роками стійкий тренд після врахування календарних ефектів?"),
        ("linear_trend", "Чи дають нахил та його інтервал гарантію майбутнього зростання, попиту або переваги над іншою мовою?"),
    ))