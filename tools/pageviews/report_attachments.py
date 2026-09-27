import hashlib
import hmac
import json
import re
import warnings
from io import BytesIO
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series
from .artifacts import MAX_ARTIFACT_BYTES, JsonArtifact
from .charts import CHART_VERSION
from .diagnostics import run_diagnostics
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .models import parse_date
from .storage import read_snapshot
from .validation import validate_response


def _same(actual: object, expected: object) -> bool:
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def _read_bytes(path: Path) -> bytes:
    with path.open("rb") as file:
        body = file.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValueError("Attachment exceeds 10 MiB.")
    return body


def _read_result(reference: JsonArtifact, operation: str) -> tuple[Path, dict]:
    path = Path(reference.path).expanduser().resolve()
    body = _read_bytes(path)
    if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), reference.sha256):
        raise ValueError("Attachment checksum mismatch.")
    result = strict_json_loads(body)
    if (type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION
            or result["operation"] != operation):
        raise ValueError("Unsupported attachment format or operation.")
    return path, result


def _context(source: dict, result: dict):
    directory = Path(result["snapshot"]).expanduser().resolve()
    entry = next((item for item in source["snapshots"] if Path(item["snapshot"]) == directory), None)
    if entry is None:
        raise ValueError("Attachment is not for an analyzed snapshot in this study.")
    snapshot = read_snapshot(directory)
    row = next(row for row in source["rows"] if row["language"] == entry["language"])
    if (snapshot.response.sha256 != entry["raw_sha256"]
            or not _same(result["request"], snapshot.request.as_dict())
            or not _same(result["source"], snapshot.response.source_metadata())
            or not _same(result["source"], row["source"])):
        raise ValueError("Attachment request or source differs from the study.")
    baseline, current = (
        Period(parse_date(source["periods"][name]["start"], "start"),
               parse_date(source["periods"][name]["end"], "end"))
        for name in ("baseline", "current")
    )
    series = validate_response(snapshot.response.body, snapshot.request)
    return entry["language"], snapshot, series, baseline, current


def _diagnostics(result: dict, snapshot, series, baseline: Period, current: Period) -> dict:
    if not _same(result["artifacts"], snapshot.artifact_paths()):
        raise ValueError("Analysis artifact locators differ from the study.")
    analysis = analyze_series(series, baseline, current, include_monthly="monthly" in result)
    if any(not _same(result.get(key), value) for key, value in analysis.items()):
        raise ValueError("Analysis periods or descriptive values differ from raw observations.")
    recorded = result["diagnostics"]
    parameters = recorded["parameters"]
    if set(parameters) != {"top_days", "trim_days", "missing_daily_upper_bound"}:
        raise ValueError("Invalid diagnostic parameters.")
    verified = run_diagnostics(series, baseline, current, **parameters)
    if not _same(recorded, verified):
        raise ValueError("Recorded diagnostics differ from raw observations.")
    # The complete saved result remains the audit source. Omit date lists from the evidence page.
    largest = verified["largest_days"]
    return {
        "diagnostics_version": verified["diagnostics_version"], "kind": verified["kind"],
        "parameters": verified["parameters"], "missing_values": verified["missing_values"],
        "largest_days": {
            "comparison_after_exclusion": largest["comparison_after_exclusion"],
            **{name: {key: value for key, value in largest[name].items()
                      if key not in {"top_days", "top_dates_truncated", "window"}}
               for name in ("baseline", "current")},
        },
        "window_edges": {key: verified["window_edges"][key]
                         for key in ("status", "reason", "affected_periods", "scenario")},
    }


def _chart(result: dict, snapshot, series, baseline: Period, current: Period) -> dict:
    analysis = analyze_series(series, baseline, current)
    periods = {name: analysis[name] for name in ("baseline", "current")}
    if (type(result["chart_version"]) is not int or result["chart_version"] != CHART_VERSION
            or result["status"] != analysis["status"] or not _same(result["periods"], periods)
            or not _same(result["method"], {"kind": "daily_line", "smoothing": None,
                                            "missing_values": "line_gaps", "seasonality_adjusted": False})):
        raise ValueError("Chart periods, method or status differ from the study.")
    path = Path(result["artifacts"]["chart"]).expanduser().resolve()
    body = _read_bytes(path)
    digest = hashlib.sha256(body).hexdigest()
    if result["chart_sha256"] != digest or not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Chart bytes or checksum changed.")
    try:
        from PIL import Image
    except ImportError as error:
        raise PageviewsError("missing_dependency", "Verifying a PNG requires Pillow from the charts extra; run uv sync --locked --extra charts.") from error
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format != "PNG":
                    raise ValueError("Expected a PNG image.")
                metadata = dict(image.info)
                image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Invalid or oversized PNG structure.") from error
    article = snapshot.request.article.replace("_", " ")
    expected = {
        "chart_version": CHART_VERSION, "article": article, "project": snapshot.request.project,
        "agent": "user", "access": "all-access", "fetched_at_utc": snapshot.response.fetched_at,
        "response_sha256": snapshot.response.sha256, "smoothing": None, "missing_values": "line_gaps",
        "periods": periods,
    }
    if (metadata.get("Title") != article or metadata.get("Source") != snapshot.response.url
            or not _same(strict_json_loads(metadata["Description"]), expected)):
        raise ValueError("Embedded PNG provenance differs from the study.")
    return {"path": str(path), "sha256": digest, "chart_version": CHART_VERSION,
            "status": analysis["status"], "visual_review_required": True}


def load_report_attachments(source: dict, *, analysis_artifacts=(), chart_artifacts=()) -> tuple[dict, list[dict]]:
    groups = (("analyze", analysis_artifacts), ("chart", chart_artifacts))
    for operation, references in groups:
        if not isinstance(references, (tuple, list)) or len(references) > len(source["rows"]):
            raise PageviewsError("invalid_request", "Supply at most one result of each operation per study language.")
        for reference in references:
            if (not isinstance(reference, JsonArtifact) or not isinstance(reference.sha256, str)
                    or re.fullmatch(r"[a-f0-9]{64}", reference.sha256) is None):
                raise PageviewsError("invalid_request", "Each attachment needs a file path and its exact lowercase SHA256.")
    evidence, audit = {}, []
    try:
        for operation, references in groups:
            field = "diagnostics" if operation == "analyze" else "chart"
            for reference in references:
                path, result = _read_result(reference, operation)
                language, snapshot, series, baseline, current = _context(source, result)
                row = evidence.setdefault(language, {})
                if field in row:
                    raise ValueError("Duplicate result for a study language and operation.")
                entry = {"operation": operation, "language": language, "path": str(path), "sha256": reference.sha256}
                if operation == "analyze":
                    row[field] = _diagnostics(result, snapshot, series, baseline, current)
                else:
                    row[field] = _chart(result, snapshot, series, baseline, current)
                    entry.update(png_path=row[field]["path"], png_sha256=row[field]["sha256"])
                audit.append(entry)
    except (PageviewsError, OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        if isinstance(error, PageviewsError) and error.code == "missing_dependency":
            raise
        raise PageviewsError(
            "report_attachment_error", "Additional report evidence is missing, changed or inconsistent; no report was produced.",
            details={"exception_type": type(error).__name__},
        ) from error
    audit.sort(key=lambda entry: (entry["language"], entry["operation"]))
    return evidence, audit
