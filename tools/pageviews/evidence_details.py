import hashlib
import hmac
import json
from datetime import date
from pathlib import Path

from .analysis import Period
from .artifacts import MAX_ARTIFACT_BYTES, JsonArtifact
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .models import parse_date
from .reports import evidence_json
from .research_state import _read_context, _reference, _verify_inputs
from .storage import read_snapshot
from .validation import validate_response

DETAIL_VERSION = 1
KINDS = {
    "observations": (50, 100),
    "missing_dates": (50, 100),
    "largest_days": (20, 50),
    "monthly_summaries": (12, 24),
    "calendar_comparisons": (4, 8),
}
RANGE_KINDS = {"observations", "missing_dates"}


def _same(actual: object, expected: object) -> bool:
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
        expected, sort_keys=True, allow_nan=False
    )


def _read_json(reference: JsonArtifact) -> dict:
    path = Path(reference.path).expanduser().resolve()
    with path.open("rb") as file:
        body = file.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES or not hmac.compare_digest(
        hashlib.sha256(body).hexdigest(), reference.sha256
    ):
        raise ValueError("Artifact size or checksum mismatch.")
    value = strict_json_loads(body)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _periods(source: dict) -> dict[str, Period]:
    return {
        name: Period(date.fromisoformat(value["start"]), date.fromisoformat(value["end"]))
        for name, value in source["periods"].items()
    }


def _study_period(day: date, periods: dict[str, Period]) -> str:
    if periods["baseline"].start <= day <= periods["baseline"].end:
        return "baseline"
    if periods["current"].start <= day <= periods["current"].end:
        return "current"
    return "between_periods"


def _page(items: list[dict], offset: int, limit: int) -> tuple[list[dict], dict]:
    total = len(items)
    if offset > total or (offset == total and total != 0):
        raise PageviewsError(
            "invalid_request", "Choose an existing detail offset.",
            details={"offset": offset, "total": total},
        )
    end = min(offset + limit, total)
    return items[offset:end], {
        "offset": offset,
        "limit": limit,
        "returned": end - offset,
        "total": total,
        "next_offset": end if end < total else None,
    }


def _base(
    reference: JsonArtifact, state: dict, source: dict, row: dict, *, kind: str,
    language: str, selection: dict, unit: str, order: str,
) -> dict:
    limitations = []
    if state["status"] == "awaiting_confirmation":
        limitations.append("pending_rules_not_evaluated")
    return {
        "operation": "evidence_detail",
        "detail_version": DETAIL_VERSION,
        "research": {
            "research_id": state["research_id"],
            "revision": state["revision"],
            "status": state["status"],
            "state_sha256": reference.sha256,
            "rules_evaluated": False,
        },
        "study": {"sha256": state["inputs"]["study"]["sha256"]},
        "language": language,
        "kind": kind,
        "status": "available",
        "reason": None,
        "unit": unit,
        "order": order,
        "selection": selection,
        "verification": {
            "status": "recomputed_and_matched",
            "source": "validated_snapshot" if row["status"] == "analyzed" else "research_state",
            "raw_sha256": row.get("source", {}).get("response_sha256"),
            "attachment_sha256": None,
        },
        "limitations": limitations,
    }


def _unavailable(result: dict, reason: str, *, offset: int, limit: int) -> dict:
    if offset != 0:
        raise PageviewsError(
            "invalid_request", "Unavailable detail has only offset zero.",
            details={"offset": offset, "total": 0},
        )
    result.update(
        status="unavailable",
        reason=reason,
        items=[],
        page={"offset": 0, "limit": limit, "returned": 0, "total": 0, "next_offset": None},
    )
    result["verification"]["status"] = "source_verified_detail_absent"
    evidence_json(result)
    return result


def _snapshot_context(source: dict, language: str):
    entry = next(item for item in source["snapshots"] if item["language"] == language)
    snapshot = read_snapshot(Path(entry["snapshot"]))
    if snapshot.response.sha256 != entry["raw_sha256"]:
        raise ValueError("Snapshot checksum differs from the verified study source.")
    return snapshot, validate_response(snapshot.response.body, snapshot.request)


def _range(start: object, end: object, periods: dict[str, Period]) -> tuple[date, date, dict]:
    if start is None or end is None:
        raise PageviewsError(
            "invalid_request", "Both start and end are required for this detail kind."
        )
    selected_start = parse_date(start, "start")
    selected_end = parse_date(end, "end")
    if selected_end < selected_start:
        raise PageviewsError("invalid_request", "The detail range end must not precede its start.")
    allowed_start = periods["baseline"].start
    allowed_end = periods["current"].end
    if selected_start < allowed_start or selected_end > allowed_end:
        raise PageviewsError(
            "invalid_request",
            "The requested detail range must be inside the pinned study window.",
            details={"allowed_start": allowed_start.isoformat(), "allowed_end": allowed_end.isoformat()},
        )
    return selected_start, selected_end, {
        "start": selected_start.isoformat(), "end": selected_end.isoformat()
    }


def _analysis_attachment(state: dict, source: dict, language: str) -> tuple[dict, str] | None:
    snapshot_path = next(
        Path(item["snapshot"]).expanduser().resolve()
        for item in source["snapshots"] if item["language"] == language
    )
    for value in state["inputs"]["analyses"]:
        reference = _reference(value)
        result = _read_json(reference)
        if Path(result.get("snapshot", "")).expanduser().resolve() == snapshot_path:
            return result, reference.sha256
    return None


def _read_detail(
    reference: JsonArtifact, state: dict, source: dict, study: dict, row: dict, *,
    language: str, kind: str, start: object, end: object, offset: int, limit: int,
) -> dict:
    periods = _periods(source)
    if kind in RANGE_KINDS:
        selected_start, selected_end, selection = _range(start, end, periods)
    else:
        if start is not None or end is not None:
            raise PageviewsError("invalid_request", "This detail kind does not accept start or end.")
        selection = {}

    units = {
        "observations": "views_per_day",
        "missing_dates": "calendar_dates",
        "largest_days": "views_per_day",
        "monthly_summaries": "views_and_views_per_observed_day",
        "calendar_comparisons": "views_per_day_and_percent",
    }
    orders = {
        "observations": "date_ascending",
        "missing_dates": "date_ascending",
        "largest_days": "baseline_then_current_rank_ascending",
        "monthly_summaries": "baseline_then_current_month_ascending",
        "calendar_comparisons": "current_month_ascending",
    }
    result = _base(
        reference, state, source, row, kind=kind, language=language,
        selection=selection, unit=units[kind], order=orders[kind],
    )
    if row["status"] != "analyzed":
        return _unavailable(result, "language_not_analyzed", offset=offset, limit=limit)

    snapshot, series = _snapshot_context(source, language)
    full_row = next(item for item in study["results"] if item["language"] == language)
    extras = {}
    if kind in RANGE_KINDS:
        selected = [day for day in series.days if selected_start <= day.day <= selected_end]
        if kind == "observations":
            items = [{
                "date": day.day.isoformat(),
                "views": day.views,
                "observation": "missing" if day.views is None else "observed",
                "study_period": _study_period(day.day, periods),
            } for day in selected]
        else:
            items = [{"date": day.day.isoformat(), "study_period": _study_period(day.day, periods)}
                     for day in selected if day.views is None]
    elif kind == "largest_days":
        attachment = _analysis_attachment(state, source, language)
        if attachment is None:
            return _unavailable(result, "diagnostics_not_supplied", offset=offset, limit=limit)
        recorded, checksum = attachment
        count = recorded["diagnostics"]["parameters"]["top_days"]
        items = []
        for name in ("baseline", "current"):
            period = periods[name]
            observed = [day for day in series.days if period.start <= day.day <= period.end and day.views is not None]
            selected = sorted(observed, key=lambda day: (-(day.views or 0), day.day))[:count]
            items.extend({"period": name, "rank": rank, "date": day.day.isoformat(), "views": day.views}
                         for rank, day in enumerate(selected, start=1))
        result["verification"]["source"] = "pinned_diagnostics_attachment"
        result["verification"]["attachment_sha256"] = checksum
        extras["parameters"] = {"top_days": count}
        extras["caveat"] = recorded["diagnostics"]["largest_days"]["caveat"]
    elif kind == "monthly_summaries":
        monthly = full_row["analysis"].get("monthly")
        if monthly is None:
            return _unavailable(result, "monthly_not_recorded", offset=offset, limit=limit)
        items = []
        for name in ("baseline", "current"):
            items.extend({"period": name, **month} for month in monthly[name])
        result["verification"]["source"] = "recorded_monthly_analysis"
    else:
        methodology = full_row["analysis"].get("methodology")
        if methodology is None or "calendar_comparison" not in methodology:
            return _unavailable(result, "calendar_comparison_not_recorded", offset=offset, limit=limit)
        calendar = methodology["calendar_comparison"]
        items = list(calendar["pairs"])
        result["verification"]["source"] = "recorded_calendar_comparison"
        extras.update(summary=calendar["summary"], caveat=calendar["caveat"])

    page_items, page = _page(items, offset, limit)
    result.update(items=page_items, page=page, **extras)
    evidence_json(result)
    return result


def read_evidence_detail(
    state: JsonArtifact, *, language: str, kind: str, start: str | None = None,
    end: str | None = None, offset: int = 0, limit: int | None = None,
) -> dict:
    """Read a bounded, verified detail page from one pinned research state."""
    if kind not in KINDS:
        raise PageviewsError("invalid_request", "Choose a supported evidence detail kind.")
    default_limit, maximum = KINDS[kind]
    if limit is None:
        limit = default_limit
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise PageviewsError(
            "invalid_request", f"offset must be nonnegative and limit must be between 1 and {maximum}."
        )
    try:
        state_value = _read_context(state)
        source, _ = _verify_inputs(state_value)
        study_reference = _reference(state_value["inputs"]["study"])
        study = _read_json(study_reference)
        rows = [row for row in source["rows"] if row["language"] == language]
        if len(rows) != 1:
            raise PageviewsError("invalid_request", "Choose a language from the pinned study.")
        return _read_detail(
            state, state_value, source, study, rows[0], language=language, kind=kind,
            start=start, end=end, offset=offset, limit=limit,
        )
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError(
            "evidence_detail_error",
            "Pinned evidence detail is missing, changed or inconsistent.",
            details={"exception_type": type(error).__name__},
        ) from error
