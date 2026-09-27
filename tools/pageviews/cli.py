import argparse
import math
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .analysis_cli import main as analyze_main
from .chart_cli import main as chart_main
from .cli_common import JsonArgumentParser, print_error, print_result
from .client import fetch_response, validate_user_agent
from .errors import PageviewsError
from .discovery_cli import main as discovery_main
from .models import build_request
from .report_cli import main as report_main
from .research_cli import main as research_main
from .storage import load_snapshot, save_snapshot
from .study_cli import main as study_main
from .topic_cli import main as topic_main
from .validation import validate_response

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "assets" / "pageviews"


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive and finite")
    return timeout


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="Download and validate daily pageviews for one confirmed article.",
        epilog="Other operations: python3 -m tools.pageviews "
        "{search,resolve,study,analyze,chart,report,research,discovery} --help",
    )
    parser.add_argument("--project", required=True, help="e.g. cs.wikipedia.org")
    parser.add_argument(
        "--article", required=True, help="Confirmed, non-URL-encoded title"
    )
    parser.add_argument("--start", required=True, help="Inclusive start, YYYY-MM-DD")
    parser.add_argument(
        "--end", required=True, help="Inclusive requested end, YYYY-MM-DD"
    )
    parser.add_argument(
        "--as-of", help="Reference UTC date, YYYY-MM-DD; defaults to today in UTC"
    )
    parser.add_argument(
        "--lag-days",
        type=int,
        default=7,
        help="Exclude this many completed UTC days in addition to today (default: 7)",
    )
    parser.add_argument(
        "--user-agent",
        required=True,
        help="Descriptive HTTP User-Agent; include real contact for ongoing use",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Snapshot storage directory",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch a new snapshot without deleting old ones",
    )
    parser.add_argument(
        "--timeout", type=_positive_timeout, default=30.0, help="HTTP timeout in seconds"
    )
    parser.add_argument(
        "--version", action="version", version=f"trend-visor {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0] == "analyze":
        return analyze_main(arguments[1:])
    if arguments and arguments[0] == "chart":
        return chart_main(arguments[1:])
    if arguments and arguments[0] == "study":
        return study_main(arguments[1:])
    if arguments and arguments[0] == "report":
        return report_main(arguments[1:])
    if arguments and arguments[0] == "research":
        return research_main(arguments[1:])
    if arguments and arguments[0] == "discovery":
        return discovery_main(arguments[1:])
    if arguments and arguments[0] in {"search", "resolve"}:
        return topic_main(arguments[0], arguments[1:])
    try:
        args = _parser().parse_args(arguments)
        user_agent = validate_user_agent(args.user_agent)
        as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
        request = build_request(
            project=args.project,
            article=args.article,
            start=args.start,
            end=args.end,
            as_of=as_of,
            lag_days=args.lag_days,
        )
        snapshot = None if args.refresh else load_snapshot(args.output_dir, request)
        cache_hit = snapshot is not None
        response = (
            snapshot.response
            if snapshot is not None
            else fetch_response(request, user_agent=user_agent, timeout=args.timeout)
        )
        series = validate_response(response.body, request)
        if snapshot is None:
            snapshot = save_snapshot(args.output_dir, request, response, series)
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": series.status,
            "request": request.as_dict(),
            "source": response.source_metadata(),
            "coverage": series.coverage_summary(),
            "cache_hit": cache_hit,
            "artifacts": snapshot.artifact_paths(),
        }
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    return 0