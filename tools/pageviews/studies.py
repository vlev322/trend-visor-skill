import json
import math
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series_zero_filled, validate_period_order
from .client import fetch_response, validate_user_agent
from .errors import PageviewsError
from .models import EARLIEST_DATE, PageviewsRequest, build_request, parse_date
from .resolutions import ResolutionPlan, ResolutionTarget
from .storage import Snapshot, load_snapshot, read_snapshot, save_snapshot
from .validation import ValidatedSeries, validate_response

STUDY_VERSION = 3
DERIVED_STUDY_VERSION = 4


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


CAVEATS = [
    "Language editions are not countries or markets; views are events, not unique people.",
    "Wikimedia's user agent classification does not guarantee the absence of bots.",
    "A confirmed item match does not establish equivalent article scope across languages.",
    "Counts follow the confirmed title; historical moves and redirect titles are not combined.",
    "Wikimedia's API omits days with zero views instead of reporting 0; this study counts "
    "each omitted day as an observed 0 (see coverage.assumed_zero_days per period).",
    "Observed differences do not establish statistical significance, persistence or product demand.",
    "A single article's traffic can move with the whole language edition, not just topic interest.",
]


def _merge_targets(
    plans: Sequence[ResolutionPlan],
) -> list[tuple[ResolutionTarget, ResolutionPlan]]:
    """Combine targets from one or more --resolution files (e.g. one Wikidata item
    per language when a topic has no single cross-language item), keeping first-seen
    language order. A language matched in more than one file is rejected rather than
    silently picking one; an unmatched language is replaced by a later matched one."""
    order: list[str] = []
    chosen: dict[str, tuple[ResolutionTarget, ResolutionPlan]] = {}
    for plan in plans:
        for target in plan.targets:
            if target.language not in chosen:
                order.append(target.language)
                chosen[target.language] = (target, plan)
                continue
            existing_target, _ = chosen[target.language]
            if existing_target.status == "matched" and target.status == "matched":
                raise PageviewsError(
                    "invalid_request",
                    f"Language {target.language!r} is matched in more than one --resolution file.",
                )
            if target.status == "matched":
                chosen[target.language] = (target, plan)
    return [chosen[language] for language in order]


def run_study(
    plans: ResolutionPlan | Sequence[ResolutionPlan],
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
) -> dict[str, object]:
    plans = (plans,) if isinstance(plans, ResolutionPlan) else tuple(plans)
    if not plans:
        raise PageviewsError("invalid_request", "At least one --resolution is required.")
    _validate_window(baseline, current, as_of, lag_days)
    if offline and refresh:
        raise PageviewsError("invalid_request", "offline and refresh cannot be combined.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "timeout must be positive and finite.")
    if not offline and user_agent is None:
        raise PageviewsError("invalid_request", "user_agent is required unless offline mode is enabled.")
    user_agent = "" if offline else validate_user_agent(user_agent or "")
    merged = _merge_targets(plans)
    requests = {
        target.language: build_request(
            project=target.project, article=target.article,
            start=baseline.start.isoformat(), end=current.end.isoformat(),
            as_of=as_of, lag_days=lag_days,
        )
        for target, _ in merged if target.status == "matched"
    }
    results = []
    blocked_by = None
    for target, plan in merged:
        row: dict[str, object] = {
            "language": target.language, "project": target.project, "article": target.article,
            "entity_id": plan.entity["entity_id"], "entity_label": plan.entity.get("label"),
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
            analysis = analyze_series_zero_filled(series, baseline, current, include_monthly=include_monthly)
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
    return {
        "schema_version": SCHEMA_VERSION, "study_version": STUDY_VERSION,
        "operation": "study", "status": status,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resolutions": [
            {"path": str(plan.path), "sha256": plan.sha256, "entity": plan.entity,
             "sources": plan.result["sources"]}
            for plan in plans
        ],
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
        "caveats": list(CAVEATS),
    }


def derive_study(
    source_study: Path,
    baseline: Period,
    current: Period,
    *,
    include_monthly: bool = False,
) -> dict[str, object]:
    """Reframe an original study's already-collected snapshots over narrower confirmed
    periods; performs zero Wikimedia HTTP requests."""
    try:
        path = source_study.expanduser().resolve()
        parent = json.loads(path.read_text(encoding="utf-8"))
        if (
            parent.get("schema_version") != SCHEMA_VERSION or parent.get("study_version") != STUDY_VERSION
            or parent.get("operation") != "study"
        ):
            raise ValueError("Only an original, non-derived study can be reframed.")
        original_baseline = Period(
            parse_date(parent["periods"]["baseline"]["start"], "baseline_start"),
            parse_date(parent["periods"]["baseline"]["end"], "baseline_end"),
        )
        original_current = Period(
            parse_date(parent["periods"]["current"]["start"], "current_start"),
            parse_date(parent["periods"]["current"]["end"], "current_end"),
        )
        validate_period_order(baseline, current)
        if (baseline.start < original_baseline.start or baseline.end > original_current.end
                or current.start < original_baseline.start or current.end > original_current.end):
            raise ValueError("Follow-up periods must remain inside the parent study's outer window.")
        as_of = parent["as_of"]
        lag_days = parent["excluded_recent_days"]
        _validate_window(baseline, current, as_of, lag_days)
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise PageviewsError(
            "invalid_request", "The source study cannot support this follow-up scope.",
            details={"exception_type": type(error).__name__},
        ) from error

    results = []
    for original_row in parent["results"]:
        if original_row["status"] != "analyzed":
            results.append(original_row)
            continue
        try:
            snapshot = read_snapshot(Path(original_row["snapshot"]))
            request = snapshot.request
            if baseline.start < request.start or current.end > request.requested_end:
                raise ValueError("Pinned snapshot does not cover the requested follow-up scope.")
            series = validate_response(snapshot.response.body, request)
            analysis = analyze_series_zero_filled(series, baseline, current, include_monthly=include_monthly)
        except PageviewsError as error:
            raise PageviewsError(
                "study_source_error", "A pinned source snapshot failed follow-up verification; no HTTP was attempted.",
                details={"language": original_row["language"], "source_code": error.code},
            ) from error
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            raise PageviewsError(
                "study_source_error", "A pinned source snapshot failed follow-up verification; no HTTP was attempted.",
                details={"language": original_row["language"], "exception_type": type(error).__name__},
            ) from error
        results.append({**original_row, "analysis": analysis, "snapshot_reused": True})

    summary = _summary(results)
    status = "complete" if summary["languages_with_complete_periods"] == len(results) else (
        "partial" if summary["analyzed_languages"] else "unavailable"
    )
    return {
        "schema_version": SCHEMA_VERSION, "study_version": DERIVED_STUDY_VERSION,
        "operation": "study", "status": status,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "pinned_snapshot_reuse",
        "source_study": {"path": str(path)},
        "resolutions": parent["resolutions"],
        "periods": {
            "baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat()},
            "current": {"start": current.start.isoformat(), "end": current.end.isoformat()},
        },
        "as_of": as_of, "excluded_recent_days": lag_days,
        "method": {
            "kind": "descriptive", "same_periods_for_all_languages": True,
            "missing_data_policy": "require_complete_periods_for_change",
            "seasonality_adjusted": False, "statistical_inference_performed": False,
        },
        "summary": summary, "results": results, "comparison": _comparison_table(results),
        "caveats": list(parent.get("caveats", CAVEATS)) + [
            "This follow-up reuses the exact parent snapshot; its recorded HTTP request window remains unchanged.",
            "Follow-up periods are a subset of the parent study window; no new network request was made.",
        ],
    }
