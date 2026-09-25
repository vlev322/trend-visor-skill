from collections.abc import Sequence
from pathlib import Path

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series
from .cli_common import JsonArgumentParser, print_error, print_result
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
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