import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from tools.pageviews.artifacts import json_output_path, save_json_artifact
from tools.pageviews.cli_common import JsonArgumentParser, print_error, print_result
from tools.pageviews.errors import PageviewsError

from .cases import DATASET_VERSION, prepare_cases
from .client import ModelClient, validate_options
from .config import load_config
from .runner import ANSWER_CONTRACT, evaluate_cases

ROOT = Path(__file__).resolve().parents[2]
METHODOLOGY_HEADING = "## Analytical methodology"


def _instructions() -> tuple[str, str]:
    try:
        body = (ROOT / "SKILL.md").read_bytes()
        text = body.decode("utf-8")
        section = METHODOLOGY_HEADING + text.split(METHODOLOGY_HEADING, 1)[1].split("\n## ", 1)[0]
    except (OSError, ValueError, IndexError) as error:
        raise PageviewsError("evaluation_setup_error", "Could not read the methodology section of SKILL.md.") from error
    return section.strip(), hashlib.sha256(body).hexdigest()


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python -m tools.model_eval",
        description="Test tool use and methodology interpretation on three synthetic cases, not the whole skill.",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="Local provider configuration; never passed to the model")
    parser.add_argument("--model", help="Explicit model override, e.g. qwen3-coder-next or gemma4")
    parser.add_argument("--dry-run", action="store_true", help="Prepare evidence and expected answers without reading credentials or using HTTP")
    parser.add_argument("--timeout", type=float, default=60.0, help="Model HTTP timeout in seconds")
    parser.add_argument("--max-output-tokens", type=int, default=1200, help="Per-response token cap, 1–4096")
    parser.add_argument("--output", type=Path, help="New JSON result; defaults to unique assets/evaluations file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        validate_options(args.timeout, args.max_output_tokens)
        output = json_output_path(args.output or ROOT / "assets" / "evaluations" / f"evaluation-{uuid4().hex}.json")
        config = None if args.dry_run else load_config(args.env_file, model=args.model)
        instructions, skill_hash = _instructions()
        cases = prepare_cases()
        if args.dry_run:
            results = [
                {"case_id": case.identifier, "status": "prepared", "model_calls": 0,
                 "question": case.question, "evidence": case.evidence, "expected": case.expected}
                for case in cases
            ]
        else:
            with ModelClient(config, timeout=args.timeout, max_tokens=args.max_output_tokens) as client:
                results = evaluate_cases(cases, client.complete, instructions)
        passed = sum(result["status"] == "passed" for result in results)
        report = {
            "schema_version": 1, "evaluation_version": 1, "dataset_version": DATASET_VERSION,
            "operation": "evaluate_model",
            "status": "prepared" if args.dry_run else ("passed" if passed == len(cases) else "failed"),
            "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": "methodology_tool_smoke_test_not_full_skill_evaluation",
            "model": config.model if config else args.model,
            "base_url": config.base_url if config else None,
            "sdk_version": version("openai") if config else None,
            "model_test_performed": not args.dry_run,
            "skill_sha256": skill_hash, "instructions": instructions, "answer_contract": ANSWER_CONTRACT,
            "parameters": {"temperature": 0, "max_output_tokens": args.max_output_tokens, "timeout": args.timeout,
                           "max_model_calls": 2 * len(cases), "automatic_retries": 0},
            "summary": {
                "cases": len(cases), "passed": passed,
                "failed": sum(result["status"] in {"failed", "error"} for result in results),
                "not_run": sum(result["status"] == "not_run" for result in results),
                "model_calls": sum(result["model_calls"] for result in results),
            },
            "results": results,
            "limitations": [
                "Three controlled fixtures test tool use and structured interpretation, not general model reliability.",
                "Only the methodology section is loaded; topic selection, live lookup, narrative reports and PDF are not evaluated.",
                "Expected numeric fields come from tested code; no LLM judge or answer-repair loop is used.",
                "A passing smoke test is not a statistical validation of the slope model or an agent-wide evaluation.",
            ],
        }
        saved = save_json_artifact(report, output)
    except PageviewsError as error:
        return print_error(error)
    print_result({
        key: report[key] for key in (
            "schema_version", "operation", "status", "scope", "model", "model_test_performed", "summary",
        )
    } | {
        "results": [{key: row[key] for key in ("case_id", "status", "reason", "failed_fields") if key in row} for row in results],
        "artifacts": {"evaluation": str(saved.path), "evaluation_sha256": saved.sha256},
    })
    return 0 if args.dry_run or passed == len(cases) else 1