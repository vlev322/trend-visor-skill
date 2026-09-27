from collections.abc import Sequence
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from uuid import uuid4

from .analysis import Period, months_periods
from .artifacts import json_output_path, save_json_artifact
from .cli_common import JsonArgumentParser, print_error, print_result
from .errors import PageviewsError
from .models import parse_date
from .resolutions import read_resolution, user_confirmed_plan
from .studies import derive_study, run_study

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python3 -m tools.pageviews study",
        description="Collect and analyze one reviewed topic across all requested languages.",
    )
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument(
        "--resolution", type=Path, action="append",
        help="Saved resolve --output JSON, confirmed by the user; repeat for languages resolved separately",
    )
    source.add_argument("--from-study", type=Path, help="Reframe an existing study over narrower periods; no HTTP")
    parser.add_argument(
        "--article", action="append", metavar="LANG:TITLE",
        help="Exact project/title the user already confirmed, bypassing resolve entirely "
        "(e.g. Wikidata is unavailable); repeat per language; combine with --resolution, not with --from-study",
    )
    parser.add_argument(
        "--months", type=int,
        help="Use the last N full calendar months vs the same months a year earlier (N<=12) "
        "or the previous N months (N>12), instead of four explicit dates",
    )
    for name in ("baseline-start", "baseline-end", "current-start", "current-end"):
        parser.add_argument(f"--{name}", help="Inclusive UTC date, YYYY-MM-DD; required unless --months is used")
    parser.add_argument("--as-of", help="Reference UTC date, YYYY-MM-DD; defaults to today in UTC")
    parser.add_argument("--lag-days", type=int, default=7, help="Exclude reference day plus N completed days (default: 7)")
    parser.add_argument("--cache-dir", type=Path, default=ASSETS / "pageviews", help="Exact-request snapshot cache")
    parser.add_argument("--user-agent", help="Descriptive identifier with real contact; defaults to TREND_VISOR_USER_AGENT")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--offline", action="store_true", help="Read only exact cached requests; never use HTTP")
    parser.add_argument("--refresh", action="store_true", help="Fetch new snapshots; preserve old snapshots")
    parser.add_argument("--monthly", action="store_true", help="Include calendar-month summaries")
    parser.add_argument("--output", type=Path, help="New study JSON; default: unique assets/studies file")
    return parser


def _parse_article(value: str) -> tuple[str, str]:
    language, separator, title = value.partition(":")
    if not separator or not language.strip() or not title.strip():
        raise PageviewsError(
            "invalid_arguments", f"--article must be LANG:TITLE, e.g. uk:Article title; got {value!r}."
        )
    return language.strip(), title.strip()


def _plans(args):
    if args.from_study is not None and (args.resolution or args.article):
        raise PageviewsError(
            "invalid_arguments", "--from-study cannot be combined with --resolution or --article."
        )
    if args.from_study is not None:
        return None
    if not args.resolution and not args.article:
        raise PageviewsError("invalid_arguments", "Supply --resolution and/or --article, or --from-study.")
    plans = [read_resolution(path) for path in (args.resolution or [])]
    if args.article:
        plans.append(user_confirmed_plan([_parse_article(value) for value in args.article]))
    return plans


def _explicit_periods(args) -> Period | None:
    values = (args.baseline_start, args.baseline_end, args.current_start, args.current_end)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise PageviewsError("invalid_arguments", "Supply all four explicit period dates, or use --months instead.")
    return (
        Period(parse_date(args.baseline_start, "baseline_start"), parse_date(args.baseline_end, "baseline_end")),
        Period(parse_date(args.current_start, "current_start"), parse_date(args.current_end, "current_end")),
    )


def _periods(args, as_of: str) -> tuple[Period, Period]:
    explicit = _explicit_periods(args)
    if args.months is not None and explicit is not None:
        raise PageviewsError("invalid_arguments", "Use either --months or explicit period dates, not both.")
    if args.months is not None:
        return months_periods(parse_date(as_of, "as_of"), args.lag_days, args.months)
    if explicit is not None:
        return explicit
    raise PageviewsError("invalid_arguments", "Supply --months or all four explicit period dates.")


def _language_summary(row: dict) -> dict:
    summary = {
        "language": row["language"], "entity_id": row.get("entity_id"),
        "resolution_status": row.get("resolution_status"),
        "article": row.get("article"), "status": row["status"], "reason": row["reason"],
    }
    analysis = row.get("analysis")
    if analysis is not None:
        baseline, current = analysis["baseline"], analysis["current"]
        summary.update(
            baseline_mean=baseline["mean_daily_views_observed"],
            current_mean=current["mean_daily_views_observed"],
            change_percent=analysis["comparison"]["change_percent"],
            change_reason=analysis["comparison"]["reason"],
            assumed_zero_days={
                "baseline": baseline["coverage"].get("assumed_zero_days", 0),
                "current": current["coverage"].get("assumed_zero_days", 0),
            },
        )
    return summary


def _compact_result(result: dict) -> dict:
    return {
        "status": result["status"], "periods": result["periods"], "as_of": result["as_of"],
        "languages": [_language_summary(row) for row in result["results"]],
        "artifacts": result["artifacts"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output = json_output_path(args.output or ASSETS / "studies" / f"study-{uuid4().hex}.json")
        plans = _plans(args)
        if plans is None:
            if args.as_of is not None or args.user_agent is not None or args.refresh or args.months is not None:
                raise PageviewsError(
                    "invalid_arguments", "--from-study reuses the parent's periods/collection settings and cannot use collection overrides."
                )
            baseline, current = _explicit_periods(args) or (None, None)
            if baseline is None:
                raise PageviewsError("invalid_arguments", "Supply all four explicit period dates with --from-study.")
            result = derive_study(args.from_study, baseline, current, include_monthly=args.monthly)
        else:
            as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
            baseline, current = _periods(args, as_of)
            user_agent = args.user_agent or environ.get("TREND_VISOR_USER_AGENT")
            result = run_study(
                plans, baseline, current, as_of=as_of, lag_days=args.lag_days,
                cache_dir=args.cache_dir, user_agent=user_agent, timeout=args.timeout,
                offline=args.offline, refresh=args.refresh, include_monthly=args.monthly,
            )
        saved = save_json_artifact(result, output)
        result["artifacts"] = {"study": str(saved.path)}
    except PageviewsError as error:
        return print_error(error)
    print_result(_compact_result(result))
    summary = result["summary"]
    return 1 if summary["failed_collections"] or summary["failed_resolution_checks"] else 0

