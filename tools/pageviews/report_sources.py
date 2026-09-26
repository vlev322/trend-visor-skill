import hashlib
import hmac
import json
import re
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series, validate_period_order
from .artifacts import MAX_ARTIFACT_BYTES
from .calendar_analysis import compare_calendar_months
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .methodology import METHODOLOGY_VERSION, MethodologyOptions
from .models import build_request, parse_date
from .resolutions import read_resolution
from .storage import read_snapshot
from .studies import STUDY_VERSION
from .validation import validate_response


def _same(actual: object, expected: object) -> bool:
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def _analyzed_row(row: dict, request, baseline: Period, current: Period) -> tuple[dict, dict]:
    snapshot = read_snapshot(Path(row["snapshot"]))
    if snapshot.request != request or not _same(row["source"], snapshot.response.source_metadata()):
        raise ValueError("Snapshot identity or source differs from the study.")
    if not _same(row["artifacts"], snapshot.artifact_paths()):
        raise ValueError("Snapshot locators differ from the study.")
    series = validate_response(snapshot.response.body, request)
    if not _same(row["coverage"], series.coverage_summary()):
        raise ValueError("Recorded coverage differs from raw observations.")
    stored = row["analysis"]
    verified = analyze_series(series, baseline, current, include_monthly="monthly" in stored)
    if any(not _same(stored.get(key), value) for key, value in verified.items()):
        raise ValueError("Recorded descriptive results differ from raw observations.")
    # Monthly arrays are verified when present, but never enter the model-facing evidence.
    verified.pop("monthly", None)
    model_recorded = False
    if "methodology" in stored:
        methodology = stored["methodology"]
        if type(methodology["methodology_version"]) is not int or methodology["methodology_version"] != METHODOLOGY_VERSION:
            raise ValueError("Unsupported recorded methodology.")
        options = MethodologyOptions(**methodology["parameters"])
        calendar = compare_calendar_months(series, baseline, current)
        if not _same(methodology["calendar_comparison"], calendar):
            raise ValueError("Recorded calendar comparison differs from raw observations.")
        verified["methodology"] = {"calendar_comparison": {"summary": calendar["summary"]}}
        model_recorded = options.trend_model is not None
    return {
        "analysis": verified, "source": snapshot.response.source_metadata(),
        "recorded_model": "not_revalidated_not_reported" if model_recorded else "not_requested",
    }, {"snapshot": str(snapshot.directory), "raw_sha256": snapshot.response.sha256}


def load_report_source(path: Path, checksum: str) -> dict:
    """Verify pinned study inputs; expose only rechecked descriptive evidence, never a new fit."""
    if not isinstance(checksum, str) or re.fullmatch(r"[a-f0-9]{64}", checksum) is None:
        raise PageviewsError("invalid_request", "Supply the exact saved study SHA256.")
    try:
        path = path.expanduser().resolve()
        with path.open("rb") as file:
            body = file.read(MAX_ARTIFACT_BYTES + 1)
        if len(body) > MAX_ARTIFACT_BYTES or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), checksum):
            raise ValueError("Study size or checksum mismatch.")
        study = strict_json_loads(body)
        if (
            type(study["schema_version"]) is not int or study["schema_version"] != SCHEMA_VERSION
            or type(study["study_version"]) is not int or study["study_version"] != STUDY_VERSION
            or study["operation"] != "study"
        ):
            raise ValueError("Unsupported study format.")
        periods = study["periods"]
        baseline, current = (
            Period(parse_date(periods[name]["start"], "start"), parse_date(periods[name]["end"], "end"))
            for name in ("baseline", "current")
        )
        validate_period_order(baseline, current)
        as_of = parse_date(study["as_of"], "as_of")
        lag = study["excluded_recent_days"]
        if type(lag) is not int or lag < 0 or (as_of - current.end).days <= lag:
            raise ValueError("Invalid recorded safety cutoff.")
        resolution = study["resolution"]
        plan = read_resolution(Path(resolution["path"]), resolution["sha256"])
        if (
            not _same(resolution["entity"], plan.entity) or not _same(resolution["sources"], plan.result["sources"])
            or resolution["confirmation"] != "explicit_file_checksum"
            or not isinstance(study["results"], list) or len(study["results"]) != len(plan.targets)
        ):
            raise ValueError("Study scope differs from the confirmed resolution.")
        rows, inputs = [], []
        for row, target in zip(study["results"], plan.targets, strict=True):
            identity = {"language": target.language, "project": target.project, "article": target.article,
                        "resolution_status": target.status}
            if any(not _same(row[key], value) for key, value in identity.items()) or not _same(row["mapping"], target.record):
                raise ValueError("Recorded article mapping changed.")
            clean = {**identity, "status": row["status"], "reason": row["reason"]}
            rows.append(clean)
            if target.status != "matched":
                if row["status"] != "not_collected" or row["reason"] != target.status or "analysis" in row:
                    raise ValueError("An unresolved article cannot have an analysis.")
                continue
            request = build_request(project=target.project, article=target.article,
                                    start=baseline.start.isoformat(), end=current.end.isoformat(),
                                    as_of=as_of.isoformat(), lag_days=lag)
            if not _same(row["request"], request.as_dict()):
                raise ValueError("Recorded request differs from study periods or scope.")
            if row["status"] == "collection_failed":
                reason = row["reason"]
                if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,80}", reason) or row["error"]["code"] != reason or "analysis" in row:
                    raise ValueError("Invalid recorded collection failure.")
                continue
            if row["status"] != "analyzed" or row["reason"] is not None:
                raise ValueError("Invalid analyzed article state.")
            evidence, source = _analyzed_row(row, request, baseline, current)
            clean.update(evidence)
            inputs.append({"language": target.language, **source})
        return {
            "path": str(path), "sha256": checksum,
            "resolution": {"path": str(plan.path), "sha256": plan.sha256, "entity_id": plan.entity["entity_id"]},
            "periods": {"baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat()},
                        "current": {"start": current.start.isoformat(), "end": current.end.isoformat()}},
            "as_of": as_of.isoformat(), "excluded_recent_days": lag, "rows": rows, "snapshots": inputs,
        }
    except (PageviewsError, OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError(
            "report_source_error", "Saved study evidence is missing, changed or inconsistent; no report was produced.",
            details={"exception_type": type(error).__name__},
        ) from error
