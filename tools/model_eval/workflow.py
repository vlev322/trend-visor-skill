from collections.abc import Sequence
import hashlib
from pathlib import Path

from tools.pageviews.artifacts import JsonArtifact, json_output_path
from tools.pageviews.cli_common import JsonArgumentParser, print_error, print_result
from tools.pageviews.errors import PageviewsError

from .client import validate_options
from .context_budget import estimate_request_context
from .workflow_runner import load_instructions, prepare_request, run_workflow
from .workflow_tools import WorkflowAdapter

ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(prog="python -m tools.model_eval.workflow",
                                description="General state-based model runner. Approvals remain host-only. No named topic profiles.")
    parser.add_argument("--kind", choices=("discovery", "research"), required=True)
    parser.add_argument("--state", nargs=2, metavar=("FILE", "SHA256"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="Fresh run directory, never an existing run")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Read-only prepared prompt; no credentials, SDK or HTTP")
    mode.add_argument("--run-model", action="store_true", help="Explicitly permit model requests; arrange provider budget/context preflight first")
    parser.add_argument("--allow-lookup", action="store_true", help="Host permits bounded Wikidata/MediaWiki metadata HTTP")
    parser.add_argument("--allow-collection", action="store_true", help="Host permits pageview HTTP after article approval; otherwise exact cache only")
    parser.add_argument("--user-agent")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "assets" / "pageviews")
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--context-window-tokens", type=int,
                        help="Provider/model input+output capacity checked by the host; required for live calls")
    parser.add_argument("--context-source",
                        help="Provider/model context and tokenizer reference checked by the host; recorded, not fetched or independently verified")
    parser.add_argument("--context-margin-tokens", type=int,
                        help="Extra safety slack (minimum 256 tokens); default is max(256 tokens, 5%% of the declared window)")
    parser.add_argument("--input-rate-per-million", help="Optional caller-supplied provider input-token rate per million")
    parser.add_argument("--output-rate-per-million", help="Optional caller-supplied provider output-token rate per million")
    parser.add_argument("--tariff-currency", help="Three-letter currency code for supplied model rates")
    parser.add_argument("--tariff-date", help="Date of the supplied provider rate, YYYY-MM-DD")
    parser.add_argument("--tariff-source", help="Provider pricing documentation/reference checked by the host")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--model", help="Explicit model override; otherwise configured model")
    try:
        args = parser.parse_args(argv)
        validate_options(args.timeout, args.max_tokens)
        if not 1 <= args.max_calls <= 40:
            parser.error("max-calls must be between 1 and 40.")
        # Validate the host declaration before touching local model configuration.
        estimate_request_context(
            [], None, model=args.model, output_reserve_tokens=args.max_tokens,
            context_window_tokens=args.context_window_tokens,
            context_source=args.context_source,
            margin_tokens=args.context_margin_tokens,
        )
        tariff_values = (args.input_rate_per_million, args.output_rate_per_million,
                         args.tariff_currency, args.tariff_date, args.tariff_source)
        if any(value is not None for value in tariff_values) and not all(value is not None for value in tariff_values):
            raise PageviewsError(
                "invalid_request", "Supply all five tariff fields together or omit them; partial prices are not assumed."
            )
        tariff = None if all(value is None for value in tariff_values) else {
            "input_per_million": args.input_rate_per_million,
            "output_per_million": args.output_rate_per_million,
            "currency": args.tariff_currency,
            "tariff_date": args.tariff_date,
            "tariff_source": args.tariff_source,
        }
        from .costing import validate_tariff
        tariff = validate_tariff(tariff)
        adapter = WorkflowAdapter(args.kind, JsonArtifact(Path(args.state[0]), args.state[1]), args.output_dir,
                                  allow_lookup=args.allow_lookup, allow_collection=args.allow_collection,
                                  user_agent=args.user_agent, cache_dir=args.cache_dir)
        json_output_path(adapter.directory / "workflow.json")
        if adapter.directory.exists() and any(adapter.directory.iterdir()):
            raise PageviewsError("output_exists", "Choose a fresh empty run directory.")
        if args.dry_run:
            instructions = load_instructions(ROOT, adapter)
            messages, tool = prepare_request(adapter, instructions)
            preflight = estimate_request_context(
                messages, tool, model=args.model, output_reserve_tokens=args.max_tokens,
                context_window_tokens=args.context_window_tokens,
                context_source=args.context_source,
                margin_tokens=args.context_margin_tokens,
            )
            if args.model is None:
                preflight["assumptions"].append("The dry-run did not read local model configuration.")
            print_result({"status": "prepared_not_executed", "model_calls": 0, "network_requests": 0,
                          "pause_status": adapter.pause_status(), "max_calls": args.max_calls, "max_output_tokens": args.max_tokens,
                          "context_preflight": preflight,
                          "instruction_sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                                 for name, text in instructions.items()},
                          "instruction_utf8_bytes": {name: len(text.encode("utf-8"))
                                                     for name, text in instructions.items()},
                          "request": {"messages": messages, "tool": tool}})
            return 0
        if adapter.pause_status():
            result = run_workflow(adapter, None, lambda current: load_instructions(ROOT, current), max_calls=args.max_calls,
                                  output_reserve_tokens=args.max_tokens,
                                  context_window_tokens=args.context_window_tokens,
                                  context_source=args.context_source,
                                  context_margin_tokens=args.context_margin_tokens,
                                  tariff=tariff)
        else:
            if args.context_window_tokens is None or args.context_source is None:
                raise PageviewsError(
                    "context_capacity_unverified",
                    "A live model call requires a host-checked model/provider context window and its source; use --dry-run until verified.",
                )
            from .client import ModelClient
            from .config import load_config

            config = load_config(args.env_file, model=args.model)
            with ModelClient(config, timeout=args.timeout, max_tokens=args.max_tokens) as client:
                result = run_workflow(adapter, client.complete,
                                      lambda current: load_instructions(ROOT, current), max_calls=args.max_calls,
                                      model_settings={"model": config.model, "base_url": config.base_url,
                                                      "timeout": args.timeout, "max_tokens": args.max_tokens,
                                                      "temperature": 0, "max_retries": 0},
                                      output_reserve_tokens=args.max_tokens,
                                      context_window_tokens=args.context_window_tokens,
                                      context_source=args.context_source,
                                      context_margin_tokens=args.context_margin_tokens,
                                      tariff=tariff)
        print_result(result)
        return 1 if result["status"] in ("failed", "budget_exhausted", "collection_incomplete") else 0
    except PageviewsError as error:
        return print_error(error)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return print_error(PageviewsError("workflow_error", "Could not prepare the workflow; no automatic retry.",
                                         details={"exception_type": type(error).__name__}))


if __name__ == "__main__":
    raise SystemExit(main())