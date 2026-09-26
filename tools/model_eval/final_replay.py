import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from tools.pageviews.analysis import Period, analyze_series, validate_period_order
from tools.pageviews.artifacts import MAX_ARTIFACT_BYTES, json_output_path, save_json_artifact
from tools.pageviews.cli_common import JsonArgumentParser, print_error, print_result
from tools.pageviews.diagnostics import run_diagnostics
from tools.pageviews.errors import PageviewsError
from tools.pageviews.json_codec import strict_json_loads
from tools.pageviews.models import normalize_article, parse_date
from tools.pageviews.storage import read_snapshot
from tools.pageviews.topic_data import language_code
from tools.pageviews.validation import validate_response as validate_series

from .client import ModelClient, validate_options
from .config import load_config
from .final_answer import (
    ANSWER_CONTRACT_VERSION, HOST_BOUND_FIELDS, final_messages, response_format, validate_response,
)
from .final_narrative import NARRATIVE_VERSION, RENDERED_FIELDS

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SavedResearch:
    path: Path
    sha256: str
    report: dict
    files: tuple[dict, ...]


def _read_bytes(path: Path) -> bytes:
    with path.open("rb") as file:
        body = file.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES:
        raise ValueError("An input artifact exceeds 10 MiB.")
    return body


def _object(body: bytes) -> dict:
    value = strict_json_loads(body)
    if not isinstance(value, dict):
        raise ValueError("Expected an artifact object.")
    return value


def _same(actual: object, expected: object) -> bool:
    return json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)


def _record(path: Path, body: bytes) -> dict:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(body).hexdigest()}


def _sidecar(directory: Path, name: str, expected: dict, files: list[dict]) -> None:
    path = directory / f"{name}-result.json"
    body = _read_bytes(path)
    if not _same(_object(body), expected):
        raise ValueError("Recorded operation differs from its saved result.")
    files.append(_record(path, body))


def _study_artifact(directory: Path, mode: str, result: dict, files: list[dict]) -> None:
    if "artifacts" not in result:
        return
    artifacts = result["artifacts"]
    path = Path(artifacts["study"]).resolve()
    if path != directory / f"study-{mode}.json":
        raise ValueError("Study output path differs from its recorded run.")
    body = _read_bytes(path)
    if hashlib.sha256(body).hexdigest() != artifacts["study_sha256"] or not _same(
        _object(body), {key: value for key, value in result.items() if key != "artifacts"},
    ):
        raise ValueError("Study artifact checksum or contents changed.")
    files.append(_record(path, body))


def _verify_article(
    row: dict, repeat: dict, results: dict, scope: dict,
    baseline: Period, current: Period, directory: Path, files: list[dict],
) -> None:
    if repeat.get("cache_hit") is not True or any(
        not _same(row.get(key), repeat.get(key))
        for key in ("language", "article", "project", "status", "reason", "resolution_status",
                    "snapshot", "source", "analysis", "request", "artifacts", "coverage")
    ):
        raise ValueError("Offline result does not reuse the original evidence.")
    snapshot = read_snapshot(Path(row["snapshot"]))
    if (
        row["resolution_status"] != "matched" or row["project"] != snapshot.request.project
        or normalize_article(row["article"]) != snapshot.request.article
        or not _same(row["request"], snapshot.request.as_dict())
        or not _same(row["source"], snapshot.response.source_metadata())
        or not _same(row["artifacts"], snapshot.artifact_paths())
        or snapshot.request.start != baseline.start or snapshot.request.end != current.end
        or snapshot.request.as_of.isoformat() != scope["as_of"]
    ):
        raise ValueError("Snapshot identity differs from the recorded study.")
    series = validate_series(snapshot.response.body, snapshot.request)
    analysis = row["analysis"]
    recomputed = analyze_series(series, baseline, current)
    if not _same(row["coverage"], series.coverage_summary()) or any(
        not _same(analysis.get(key), value) for key, value in recomputed.items()
    ):
        raise ValueError("Recorded descriptive analysis disagrees with the raw observations.")
    for path in snapshot.artifact_paths().values():
        files.append(_record(Path(path), _read_bytes(Path(path))))
    detailed = results[f"analyze_{row['language']}"]
    chart = results[f"chart_{row['language']}"]
    if detailed.get("operation") != "analyze" or chart.get("operation") != "chart":
        raise ValueError("Missing analysis or chart operation.")
    for operation in (detailed, chart):
        if any(not _same(operation.get(key), row[key]) for key in ("snapshot", "request", "source")):
            raise ValueError("Analysis or chart source differs from the selected snapshot.")
    if any(not _same(detailed.get(key), value) for key, value in analysis.items()):
        raise ValueError("Detailed and study analysis disagree.")
    parameters = detailed["diagnostics"]["parameters"]
    diagnostics = run_diagnostics(
        series, baseline, current, top_days=parameters["top_days"], trim_days=parameters["trim_days"],
        missing_daily_upper_bound=parameters["missing_daily_upper_bound"],
    )
    if not _same(diagnostics, detailed["diagnostics"]):
        raise ValueError("Saved sensitivity checks disagree with observations.")
    if chart["periods"] != {key: analysis[key] for key in ("baseline", "current")}:
        raise ValueError("Chart periods differ from the analysis.")
    png = Path(chart["artifacts"]["chart"]).resolve()
    if png != directory / f"{row['language']}-daily.png":
        raise ValueError("Chart path differs from the recorded run.")
    body = _read_bytes(png)
    if not body.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(body).hexdigest() != chart["chart_sha256"]:
        raise ValueError("Saved PNG changed or is invalid.")
    files.append(_record(png, body))


def load_research(path: Path, checksum: str) -> SavedResearch:
    if not isinstance(checksum, str) or re.fullmatch(r"[a-f0-9]{64}", checksum) is None:
        raise PageviewsError("invalid_request", "Supply the exact saved live-test SHA256.")
    try:
        path = path.expanduser().resolve()
        body = _read_bytes(path)
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), checksum):
            raise ValueError("Live-test checksum mismatch.")
        report = _object(body)
        if report.get("phase") != "research" or report.get("status") not in {"failed", "completed"}:
            raise ValueError("Only a recorded research phase can supply final-answer evidence.")
        scope = report["scope"]
        languages = scope["languages"]
        if not isinstance(languages, list) or not 1 <= len(languages) <= 50 or any(
            language_code(value) != value for value in languages
        ) or len(languages) != len(set(languages)):
            raise ValueError("Invalid recorded language scope.")
        periods = scope["periods"]
        baseline = Period(parse_date(periods["baseline-start"], "baseline-start"),
                          parse_date(periods["baseline-end"], "baseline-end"))
        current = Period(parse_date(periods["current-start"], "current-start"),
                         parse_date(periods["current-end"], "current-end"))
        validate_period_order(baseline, current)
        results = report["operation_results"]
        fresh, offline = results["study_fresh"], results["study_offline"]
        for study in (fresh, offline):
            if study.get("operation") != "study" or [row["language"] for row in study["results"]] != languages:
                raise ValueError("Study language scope changed.")
        if not _same(fresh["comparison"], offline["comparison"]):
            raise ValueError("Offline comparison differs from the original study.")
        analyzed = [row["language"] for row in fresh["results"] if row["status"] == "analyzed"]
        required = {"study_fresh", "study_offline"} | {
            f"{operation}_{language}" for operation in ("analyze", "chart") for language in analyzed
        }
        if not analyzed or set(results) != required:
            raise ValueError("The data workflow did not finish before final answering.")
        files = [_record(path, body)]
        for name, result in results.items():
            _sidecar(path.parent, name, result, files)
        _study_artifact(path.parent, "fresh", fresh, files)
        _study_artifact(path.parent, "offline", offline, files)
        for row, repeat in zip(fresh["results"], offline["results"], strict=True):
            if row["status"] == "analyzed":
                _verify_article(row, repeat, results, scope, baseline, current, path.parent, files)
            elif row["status"] != "not_collected" or not _same(row, repeat):
                raise ValueError("Unresolved language outcome differs across saved operations.")
        return SavedResearch(path, checksum, report, tuple(files))
    except (PageviewsError, OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
        raise PageviewsError(
            "replay_source_error", "Saved research evidence is missing, changed or inconsistent; no model request was made.",
            details={"exception_type": type(error).__name__},
        ) from error


def run_replay(saved: SavedResearch, complete: Callable, skill: str) -> dict:
    results = saved.report["operation_results"]
    request = {"messages": final_messages(saved.report["scope"], results, skill),
               "response_format": response_format()}
    report = {
        "schema_version": 1, "replay_version": 1, "operation": "replay_final_answer",
        "status": "failed", "answer_contract_version": ANSWER_CONTRACT_VERSION,
        "source_live": {"path": str(saved.path), "sha256": saved.sha256, "status": saved.report["status"],
                        "model": saved.report.get("model"), "skill_sha256": saved.report.get("skill_sha256")},
        "verified_input_files": list(saved.files), "skill_sha256": hashlib.sha256(skill.encode()).hexdigest(),
        "request": request, "model_calls": 1, "wikimedia_requests": 0,
        "limits": {"model_calls": 1, "automatic_retries": 0, "tools_available": False},
        "original_test_regraded": False, "narrative_review_required": True,
        "host_bound_fields": list(HOST_BOUND_FIELDS),
        "host_rendered_fields": list(RENDERED_FIELDS), "narrative_renderer_version": NARRATIVE_VERSION,
        "scope": "final_answer_only_on_preserved_evidence_not_full_workflow_retest",
    }
    try:
        response = complete(request["messages"], None, response_format=request["response_format"])
        report["response"] = response
        answer = validate_response(response, results)
        report.update(status="completed", facts_verified=True, interpretations_verified=True, answer=answer)
    except PageviewsError as error:
        report["error"] = error.as_dict()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Retest only the final structured answer using verified saved research.")
    parser.add_argument("--live", type=Path, required=True, help="Exact saved research live.json")
    parser.add_argument("--live-sha256", required=True, help="Checksum from the original live-test result")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--model", default="qwen3-coder-next")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--output", type=Path, help="New replay JSON; existing files are never replaced")
    try:
        args = parser.parse_args(argv)
        validate_options(args.timeout, args.max_output_tokens)
        output = json_output_path(args.output or ROOT / "assets" / "evaluations" / f"final-replay-{uuid4().hex}.json")
        saved = load_research(args.live, args.live_sha256)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        config = load_config(args.env_file, model=args.model)
        with ModelClient(config, timeout=args.timeout, max_tokens=args.max_output_tokens) as client:
            report = run_replay(saved, client.complete, skill)
        report.update(
            model=config.model, base_url=config.base_url,
            created_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            parameters={"temperature": 0, "max_output_tokens": args.max_output_tokens, "timeout": args.timeout},
        )
        artifact = save_json_artifact(report, output)
    except PageviewsError as error:
        return print_error(error)
    except (OSError, ValueError) as error:
        return print_error(PageviewsError("replay_io_error", "Could not access replay files.",
                                         details={"exception_type": type(error).__name__}))
    summary = {key: report[key] for key in (
        "status", "operation", "scope", "model", "model_calls", "wikimedia_requests",
        "answer_contract_version", "original_test_regraded", "narrative_review_required",
        "host_bound_fields",
        "host_rendered_fields",
    )}
    for field in ("answer", "error", "facts_verified", "interpretations_verified"):
        if field in report:
            summary[field] = report[field]
    summary["artifacts"] = {"replay": str(artifact.path), "replay_sha256": artifact.sha256}
    print_result(summary)
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())