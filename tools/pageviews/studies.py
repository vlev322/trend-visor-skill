import math
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series, validate_period_order
from .client import fetch_response, validate_user_agent
from .errors import PageviewsError
from .methodology import MethodologyOptions, assess_methodology, prepare_methodology
from .models import EARLIEST_DATE, PageviewsRequest, build_request, parse_date
from .resolutions import ResolutionPlan
from .storage import Snapshot, load_snapshot, save_snapshot
from .validation import ValidatedSeries, validate_response

STUDY_VERSION = 1


def _validate_window(baseline: Period, current: Period, as_of: str, lag_days: int) -> None:
    validate_period_order(baseline, current)
    reference = parse_date(as_of, "as_of")
    if baseline.start < EARLIEST_DATE:
        raise PageviewsError("invalid_request", "Pageviews start on 2015-07-01.")
    if type(lag_days) is not int or lag_days < 0:
        raise PageviewsError("invalid_request", "lag_days must be a nonnegative integer.")
    if (reference - current.end).days <= lag_days:
        raise PageviewsError(
            "invalid_request",
            "The current period crosses the safety cutoff. Choose explicit earlier periods; "
            "study never clips comparison windows.",
        )


def _collect_snapshot(
    request: PageviewsRequest,
    cache_dir: Path,
    *,
    user_agent: str,
    timeout: float,
    offline: bool,
    refresh: bool,
    blocked_by: dict | None,
) -> tuple[Snapshot, ValidatedSeries, bool]:
    snapshot = None if refresh else load_snapshot(cache_dir, request)
    cache_hit = snapshot is not None
    if snapshot is None:
        if offline:
            raise PageviewsError(
                "cache_miss", "No snapshot for this exact request; offline mode cannot fetch it."
            )
        if blocked_by is not None:
            raise PageviewsError(
                "collection_stopped", "No HTTP request made after an earlier service limit or denial.",
                details={"blocked_by": blocked_by},
            )
        response = fetch_response(request, user_agent=user_agent, timeout=timeout)
        series = validate_response(response.body, request)
        snapshot = save_snapshot(cache_dir, request, response, series)
    else:
        series = validate_response(snapshot.response.body, request)
    return snapshot, series, cache_hit


def _summary(results: list[dict[str, object]]) -> dict[str, int]:
    analyzed = [row for row in results if row["status"] == "analyzed"]
    return {
        "requested_languages": len(results),
        "matched_articles": sum(row["resolution_status"] == "matched" for row in results),
        "analyzed_languages": len(analyzed),
        "languages_with_complete_periods": sum(
            row["analysis"]["status"] == "complete" for row in analyzed
        ),
        "computed_changes": sum(
            row["analysis"]["comparison"]["status"] == "computed" for row in analyzed
        ),
        "failed_collections": sum(row["status"] == "collection_failed" for row in results),
        "failed_resolution_checks": sum(
            row["resolution_status"] in {"check_failed", "not_checked"} for row in results
        ),
    }


def _comparison_row(result: dict[str, object]) -> dict[str, object]:
    analysis = result.get("analysis", {})
    baseline = analysis.get("baseline", {})
    current = analysis.get("current", {})
    comparison = analysis.get("comparison", {})
    return {
        "language": result["language"], "project": result["project"],
        "article": result["article"],
        "status": comparison.get("status", "not_computed"),
        "reason": comparison.get("reason", result["reason"]),
        "baseline_mean_daily_views_observed": baseline.get("mean_daily_views_observed"),
        "current_mean_daily_views_observed": current.get("mean_daily_views_observed"),
        "baseline_coverage": baseline.get("coverage"),
        "current_coverage": current.get("coverage"),
        "change_percent": comparison.get("change_percent"),
    }


def _comparison_table(results: list[dict[str, object]]) -> dict[str, object]:
    rows = [_comparison_row(result) for result in results]
    eligible = [row["language"] for row in rows if row["status"] == "computed"]
    excluded = [
        {"language": row["language"], "reason": row["reason"]}
        for row in rows if row["status"] != "computed"
    ]
    return {
        "kind": "descriptive", "metric": "change_in_mean_daily_views", "unit": "percent",
        "status": "available" if len(eligible) >= 2 else "not_computed",
        "reason": None if len(eligible) >= 2 else "fewer_than_two_comparable_languages",
        "scope": "eligible_subset" if excluded else "all_requested_languages",
        "order": "requested_languages", "statistical_inference_performed": False,
        "eligible_languages": eligible, "excluded_languages": excluded, "rows": rows,
    }


def run_study(
    plan: ResolutionPlan,
    baseline: Period,
    current: Period,
    *,
    as_of: str,
    cache_dir: Path,
    lag_days: int = 7,
    user_agent: str | None = None,
    timeout: float = 30.0,
    offline: bool = False,
    refresh: bool = False,
    include_monthly: bool = False,
    methodology: MethodologyOptions | None = None,
) -> dict[str, object]:
    _validate_window(baseline, current, as_of, lag_days)
    prepare_methodology(methodology, baseline, current)
    if offline and refresh:
        raise PageviewsError("invalid_request", "offline and refresh cannot be combined.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "timeout must be positive and finite.")
    if not offline and user_agent is None:
        raise PageviewsError("invalid_request", "user_agent is required unless offline mode is enabled.")
    user_agent = "" if offline else validate_user_agent(user_agent or "")
    requests = {
        target.language: build_request(
            project=target.project, article=target.article,
            start=baseline.start.isoformat(), end=current.end.isoformat(),
            as_of=as_of, lag_days=lag_days,
        )
        for target in plan.targets if target.status == "matched"
    }
    results = []
    blocked_by = None
    for target in plan.targets:
        row: dict[str, object] = {
            "language": target.language, "project": target.project, "article": target.article,
            "resolution_status": target.status, "mapping": target.record,
            "status": "not_collected", "reason": target.status,
        }
        results.append(row)
        if target.status != "matched":
            continue
        request = requests[target.language]
        row["request"] = request.as_dict()
        try:
            snapshot, series, cache_hit = _collect_snapshot(
                request, cache_dir, user_agent=user_agent, timeout=timeout,
                offline=offline, refresh=refresh, blocked_by=blocked_by,
            )
            analysis = analyze_series(series, baseline, current, include_monthly=include_monthly)
            if methodology is not None:
                analysis["methodology"] = assess_methodology(
                    series, baseline, current, options=methodology,
                )
        except PageviewsError as error:
            row.update(status="collection_failed", reason=error.code, error=error.as_dict())
            if (
                error.code in {"rate_limited", "api_busy"}
                or error.details.get("http_status") in {403, 429, 503}
            ):
                blocked_by = {"language": target.language, "error": error.as_dict()}
            continue
        row.update(
            status="analyzed", reason=None, cache_hit=cache_hit,
            snapshot=str(snapshot.directory.resolve()), artifacts=snapshot.artifact_paths(),
            source=snapshot.response.source_metadata(), coverage=series.coverage_summary(),
            analysis=analysis,
        )
    summary = _summary(results)
    status = "complete" if summary["languages_with_complete_periods"] == len(results) else (
        "partial" if summary["analyzed_languages"] else "unavailable"
    )
    result = {
        "schema_version": SCHEMA_VERSION, "study_version": STUDY_VERSION,
        "operation": "study", "status": status,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resolution": {
            "path": str(plan.path), "sha256": plan.sha256, "entity": plan.entity,
            "sources": plan.result["sources"], "confirmation": "explicit_file_checksum",
        },
        "periods": {
            "baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat()},
            "current": {"start": current.start.isoformat(), "end": current.end.isoformat()},
        },
        "as_of": as_of, "excluded_recent_days": lag_days,
        "mode": "offline" if offline else ("refresh" if refresh else "cache_or_fetch"),
        "method": {
            "kind": "descriptive", "same_periods_for_all_languages": True,
            "missing_data_policy": "require_complete_periods_for_change",
            "seasonality_adjusted": False, "statistical_inference_performed": False,
        },
        "summary": summary, "results": results, "comparison": _comparison_table(results),
        "caveats": [
            "Language editions are not countries or markets; views are events, not unique people.",
            "Wikimedia's user agent classification does not guarantee the absence of bots.",
            "A confirmed item match does not establish equivalent article scope across languages.",
            "Counts follow the confirmed title; historical moves and redirect titles are not combined.",
            "Missing mappings or observations are unknown, not evidence of zero audience interest.",
            "Observed differences do not establish statistical significance, persistence or product demand.",
        ],
    }
    if methodology is not None:
        modeled = [
            row["analysis"]["methodology"]["trend_model"]
            for row in results if row["status"] == "analyzed"
        ]
        result["methodology"] = {
            "methodology_version": 1, "parameters": methodology.as_dict(),
            "models_fitted": sum(model["status"] == "computed" for model in modeled),
            "models_with_intervals": sum(model.get("statistical_inference_performed", False) for model in modeled),
            "inference_scope": "individual_articles_not_between_language_differences",
            "parent_method_scope": "descriptive_period_summaries_and_mean_comparison",
            "comparison_table_unchanged": True,
        }
    return result