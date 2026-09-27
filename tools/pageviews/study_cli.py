from collections.abc import Sequence
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from uuid import uuid4

from .analysis import Period
from .artifacts import json_output_path, save_json_artifact
from .cli_common import JsonArgumentParser, print_error, print_result
from .errors import PageviewsError
from .models import parse_date
from .resolutions import read_resolution
from .studies import derive_study, run_study

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python3 -m tools.pageviews study",
        description="Collect and analyze one reviewed topic across all requested languages.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--resolution", type=Path, help="Saved resolve --output JSON, confirmed by the user")
    source.add_argument("--from-study", type=Path, help="Reframe an existing study over narrower periods; no HTTP")
    for name in ("baseline-start", "baseline-end", "current-start", "current-end"):
        parser.add_argument(f"--{name}", required=True, help="Inclusive UTC date, YYYY-MM-DD")
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        output = json_output_path(args.output or ASSETS / "studies" / f"study-{uuid4().hex}.json")
        baseline = Period(
            parse_date(args.baseline_start, "baseline_start"),
            parse_date(args.baseline_end, "baseline_end"),
        )
        current = Period(
            parse_date(args.current_start, "current_start"),
            parse_date(args.current_end, "current_end"),
        )
        if args.from_study:
            if args.as_of is not None or args.user_agent is not None or args.refresh:
                raise PageviewsError(
                    "invalid_arguments", "--from-study reuses the parent's periods/collection settings and cannot use collection overrides."
                )
            result = derive_study(args.from_study, baseline, current, include_monthly=args.monthly)
        else:
            as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
            user_agent = args.user_agent or environ.get("TREND_VISOR_USER_AGENT")
            plan = read_resolution(args.resolution)
            result = run_study(
                plan, baseline, current, as_of=as_of, lag_days=args.lag_days,
                cache_dir=args.cache_dir, user_agent=user_agent, timeout=args.timeout,
                offline=args.offline, refresh=args.refresh, include_monthly=args.monthly,
            )
        saved = save_json_artifact(result, output)
        result["artifacts"] = {"study": str(saved.path), "study_sha256": saved.sha256}
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    summary = result["summary"]
    return 1 if summary["failed_collections"] or summary["failed_resolution_checks"] else 0
