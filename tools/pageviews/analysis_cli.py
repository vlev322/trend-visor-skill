from collections.abc import Sequence
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series
from .cli_common import JsonArgumentParser, print_error, print_result
from .diagnostics import DEFAULT_TOP_DAYS, DEFAULT_TRIM_DAYS, run_diagnostics
from .errors import PageviewsError
from .models import parse_date
from .storage import read_snapshot
from .validation import validate_response


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python3 -m tools.pageviews analyze",
        description="Describe two explicit periods in a saved snapshot, without HTTP.",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        required=True,
        help="Exact directory containing raw.json, series.json and metadata.json",
    )
    for name in ("baseline", "current"):
        parser.add_argument(
            f"--{name}-start", required=True, help="Inclusive start, YYYY-MM-DD"
        )
        parser.add_argument(
            f"--{name}-end", required=True, help="Inclusive end, YYYY-MM-DD"
        )
    parser.add_argument(
        "--monthly", action="store_true", help="Include calendar-month summaries"
    )
    parser.add_argument(
        "--diagnostics", action="store_true",
        help="Include sensitivity scenarios; not statistical confidence estimates",
    )
    parser.add_argument(
        "--top-days", type=int,
        help="Largest days to exclude per period in a scenario "
        f"(default: {DEFAULT_TOP_DAYS})",
    )
    parser.add_argument(
        "--trim-days", type=int,
        help=f"Calendar days to trim from each period edge (default: {DEFAULT_TRIM_DAYS})",
    )
    parser.add_argument(
        "--missing-daily-upper-bound", type=int,
        help="Assume missing counts lie between 0 and this value; no default cap",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = _parser()
        args = parser.parse_args(argv)
        supplied_scenarios = (
            args.top_days, args.trim_days, args.missing_daily_upper_bound
        )
        if not args.diagnostics and any(
            value is not None for value in supplied_scenarios
        ):
            parser.error("Scenario parameters require --diagnostics.")
        baseline = Period(
            parse_date(args.baseline_start, "baseline-start"),
            parse_date(args.baseline_end, "baseline-end"),
        )
        current = Period(
            parse_date(args.current_start, "current-start"),
            parse_date(args.current_end, "current-end"),
        )
        snapshot = read_snapshot(args.snapshot)
        series = validate_response(snapshot.response.body, snapshot.request)
        analysis = analyze_series(
            series, baseline, current, include_monthly=args.monthly
        )
        if args.diagnostics:
            analysis["diagnostics"] = run_diagnostics(
                series, baseline, current,
                top_days=(
                    args.top_days if args.top_days is not None else DEFAULT_TOP_DAYS
                ),
                trim_days=(
                    args.trim_days if args.trim_days is not None else DEFAULT_TRIM_DAYS
                ),
                missing_daily_upper_bound=args.missing_daily_upper_bound,
            )
        result = {
            "schema_version": SCHEMA_VERSION,
            "operation": "analyze",
            "snapshot": str(snapshot.directory),
            "request": snapshot.request.as_dict(),
            "source": snapshot.response.source_metadata(),
            "artifacts": snapshot.artifact_paths(),
            **analysis,
        }
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    return 0