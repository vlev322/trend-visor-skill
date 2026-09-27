import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series, validate_period_order
from .artifacts import MAX_ARTIFACT_BYTES, JsonArtifact
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .methodology import MethodologyOptions, assess_methodology, prepare_methodology
from .models import parse_date
from .storage import read_snapshot
from .studies import DERIVED_STUDY_VERSION, _comparison_table, _summary, _validate_window
from .validation import validate_response


def derive_study_from_pinned_source(
    source_study: JsonArtifact,
    baseline: Period,
    current: Period,
    *,
    follow_up_confirmation: str,
    include_monthly: bool = False,
    methodology: MethodologyOptions | None = None,
) -> dict[str, object]:
    """Create a new study over confirmed subperiods using only a verified parent snapshot."""
    from .report_sources import load_report_source

    if not isinstance(source_study, JsonArtifact):
        raise PageviewsError("invalid_request", "A pinned source study reference is required.")
    if (not isinstance(follow_up_confirmation, str) or not follow_up_confirmation.strip()
            or len(follow_up_confirmation) > 2000
            or any(ord(char) < 32 for char in follow_up_confirmation)):
        raise PageviewsError("invalid_request", "Record the actual single-line follow-up confirmation.")
    if type(include_monthly) is not bool:
        raise PageviewsError("invalid_request", "include_monthly must be boolean.")

    verified = load_report_source(source_study.path, source_study.sha256)
    try:
        path = source_study.path.expanduser().resolve()
        with path.open("rb") as file:
            body = file.read(MAX_ARTIFACT_BYTES + 1)
        if (len(body) > MAX_ARTIFACT_BYTES
                or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), source_study.sha256)):
            raise ValueError("Parent study changed after verification.")
        parent = strict_json_loads(body)
        if (parent["study_version"] != 1 or "source_study" in parent
                or len(parent["results"]) != len(verified["rows"])):
            raise ValueError("Only an original non-derived study can be reframed.")
        original_baseline = Period(
            parse_date(verified["periods"]["baseline"]["start"], "baseline_start"),
            parse_date(verified["periods"]["baseline"]["end"], "baseline_end"),
        )
        original_current = Period(
            parse_date(verified["periods"]["current"]["start"], "current_start"),
            parse_date(verified["periods"]["current"]["end"], "current_end"),
        )
        validate_period_order(baseline, current)
        if (baseline.start < original_baseline.start or baseline.end > original_current.end
                or current.start < original_baseline.start or current.end > original_current.end):
            raise ValueError("Follow-up periods must remain inside the parent study's approved outer window.")
        as_of = verified["as_of"]
        lag_days = verified["excluded_recent_days"]
        _validate_window(baseline, current, as_of, lag_days)
        prepare_methodology(methodology, baseline, current)
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise PageviewsError(
            "invalid_request", "The pinned source study cannot support this follow-up scope.",
            details={"exception_type": type(error).__name__},
        ) from error

    if not verified["rows"] or any(row["status"] != "analyzed" for row in verified["rows"]):
        raise PageviewsError(
            "invalid_request", "Follow-up derivation requires every parent article to have a verified snapshot."
        )

    original_rows = parent["results"]
    results = []
    for source_row, original_row in zip(verified["rows"], original_rows, strict=True):
        try:
            if source_row["language"] != original_row["language"]:
                raise ValueError("Parent rows changed order or identity.")
            snapshot = read_snapshot(Path(original_row["snapshot"]))
            request = snapshot.request
            request_window_start = request.start
            request_window_end = request.requested_end
            if (request.project != source_row["project"] or request.article.replace("_", " ") != source_row["article"]
                    or request.as_of.isoformat() != as_of or request.lag_days != lag_days
                    or baseline.start < request_window_start or current.end > request_window_end):
                raise ValueError("Pinned snapshot does not contain the approved follow-up scope.")
            series = validate_response(snapshot.response.body, request)
            analysis = analyze_series(series, baseline, current, include_monthly=include_monthly)
            if methodology is not None:
                analysis["methodology"] = assess_methodology(
                    series, baseline, current, options=methodology,
                )
            results.append({
                "language": source_row["language"],
                "project": source_row["project"],
                "article": source_row["article"],
                "resolution_status": "matched",
                "mapping": original_row["mapping"],
                "status": "analyzed",
                "reason": None,
                "request": request.as_dict(),
                "snapshot": str(snapshot.directory),
                "artifacts": snapshot.artifact_paths(),
                "source": snapshot.response.source_metadata(),
                "coverage": series.coverage_summary(),
                "analysis": analysis,
                "snapshot_reused": True,
            })
        except PageviewsError as error:
            raise PageviewsError(
                "study_source_error", "A pinned source snapshot failed follow-up verification; no HTTP was attempted.",
                details={"language": source_row["language"], "source_code": error.code},
            ) from error
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            raise PageviewsError(
                "study_source_error", "A pinned source snapshot failed follow-up verification; no HTTP was attempted.",
                details={"language": source_row["language"], "exception_type": type(error).__name__},
            ) from error

    summary = _summary(results)
    status = "complete" if summary["languages_with_complete_periods"] == len(results) else "partial"
    return {
        "schema_version": SCHEMA_VERSION,
        "study_version": DERIVED_STUDY_VERSION,
        "operation": "study",
        "status": status,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "pinned_snapshot_reuse",
        "source_study": {
            "path": str(source_study.path.expanduser().resolve()),
            "sha256": source_study.sha256,
            "user_reply": follow_up_confirmation,
        },
        "resolution": parent["resolution"],
        "periods": {
            "baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat()},
            "current": {"start": current.start.isoformat(), "end": current.end.isoformat()},
        },
        "as_of": as_of,
        "excluded_recent_days": lag_days,
        "method": {
            "kind": "descriptive",
            "same_periods_for_all_languages": True,
            "missing_data_policy": "require_complete_periods_for_change",
            "seasonality_adjusted": False,
            "statistical_inference_performed": False,
        },
        "summary": summary,
        "results": results,
        "comparison": _comparison_table(results),
        "caveats": list(parent["caveats"]) + [
            "This follow-up reuses the exact checksum-verified parent snapshot; its recorded HTTP request window remains unchanged.",
            "Follow-up periods are a user-confirmed subset of the parent study window; no new network request was made.",
        ],
    }