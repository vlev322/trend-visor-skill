import hashlib
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from . import SCHEMA_VERSION
from .analysis import Period
from .charts import CHART_VERSION, ChartSource, prepare_chart_data, render_png, save_png
from .cli_common import JsonArgumentParser, print_error, print_result
from .errors import PageviewsError
from .models import parse_date
from .storage import read_snapshot
from .validation import validate_response

DEFAULT_OUTPUT_DIRECTORY = Path(__file__).resolve().parents[2] / "assets" / "charts"


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python3 -m tools.pageviews chart",
        description="Plot unsmoothed daily pageviews from a saved snapshot, without HTTP.",
    )
    parser.add_argument(
        "--snapshot", type=Path, required=True,
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
        "--output", type=Path,
        help="New PNG path outside snapshots; defaults to a unique file in assets/charts",
    )
    return parser


def _output_path(requested: Path | None, snapshot_directory: Path) -> Path:
    path = requested or DEFAULT_OUTPUT_DIRECTORY / f"pageviews-{uuid4().hex}.png"
    path = path.expanduser()
    if path.is_symlink():
        raise PageviewsError("output_exists", "Output path is a symlink; choose a new path.")
    path = path.resolve()
    if path.suffix.lower() != ".png":
        raise PageviewsError("invalid_request", "The chart output must use a .png extension.")
    for parent in path.parents:
        if parent == snapshot_directory or all(
            (parent / name).is_file() for name in ("raw.json", "series.json", "metadata.json")
        ):
            raise PageviewsError(
                "invalid_request", "Save charts outside snapshot directories."
            )
    if path.exists():
        raise PageviewsError(
            "output_exists", "Chart output already exists; choose a new filename.",
            details={"path": str(path)},
        )
    return path


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
        periods = prepare_chart_data(series, baseline, current)
        output = _output_path(args.output, snapshot.directory)
        source = ChartSource(
            article=snapshot.request.article.replace("_", " "),
            project=snapshot.request.project,
            url=snapshot.response.url,
            fetched_at=snapshot.response.fetched_at,
            response_sha256=snapshot.response.sha256,
        )
        png = render_png(periods, source)
        output = save_png(png, output)
        observed = sum(period.summary.observed_days for period in periods)
        missing = sum(period.summary.missing_days for period in periods)
        status = "no_observations" if observed == 0 else ("partial" if missing else "complete")
        result = {
            "schema_version": SCHEMA_VERSION,
            "operation": "chart",
            "chart_version": CHART_VERSION,
            "status": status,
            "snapshot": str(snapshot.directory),
            "request": snapshot.request.as_dict(),
            "source": snapshot.response.source_metadata(),
            "periods": {
                period.name.lower(): period.summary.as_dict() for period in periods
            },
            "method": {
                "kind": "daily_line",
                "smoothing": None,
                "missing_values": "line_gaps",
                "seasonality_adjusted": False,
            },
            "artifacts": {"chart": str(output)},
            "chart_sha256": hashlib.sha256(png).hexdigest(),
        }
    except PageviewsError as error:
        return print_error(error)
    except OSError as error:
        return print_error(
            PageviewsError(
                "chart_write_error", "Could not access the chart output location.",
                details={"exception_type": type(error).__name__},
            )
        )
    print_result(result)
    return 0