from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from .analysis import Period
from .artifacts import JsonArtifact, json_output_path, save_json_artifact
from .cli_common import JsonArgumentParser, print_error, print_result
from .errors import PageviewsError
from .methodology_cli import add_methodology_arguments, methodology_options
from .models import parse_date
from .resolutions import read_resolution
from .studies import run_study
from .study_derivation import derive_study_from_pinned_source

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="python3 -m tools.pageviews study",
        description="Collect and analyze one reviewed topic across all requested languages.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--resolution", type=Path, help="Saved resolve --output JSON")
    source.add_argument("--from-study", nargs=2, metavar=("FILE", "SHA256"),
                        help="Create a follow-up study from an exact verified parent study; no HTTP")
    parser.add_argument("--confirm-sha256", help="Exact resolution checksum; required with --resolution")
    parser.add_argument("--follow-up-confirmation", help="Actual human confirmation of new periods; required with --from-study")
    for name in ("baseline-start", "baseline-end", "current-start", "current-end"):
        parser.add_argument(f"--{name}", required=True, help="Inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--as-of", help="Explicit reference UTC date, YYYY-MM-DD")
    parser.add_argument("--lag-days", type=int, help="Exclude reference day plus N completed days (default: 7)")
    parser.add_argument("--cache-dir", type=Path, default=ASSETS / "pageviews", help="Exact-request snapshot cache")
    parser.add_argument("--user-agent", help="Required online: descriptive identifier with real contact")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--offline", action="store_true", help="Read only exact cached requests; never use HTTP")
    parser.add_argument("--refresh", action="store_true", help="Fetch new snapshots; preserve old snapshots")
    parser.add_argument("--monthly", action="store_true", help="Include calendar-month summaries")
    parser.add_argument("--output", type=Path, help="New study JSON; default: unique assets/studies file")
    add_methodology_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        methodology = methodology_options(args)
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
            if (args.confirm_sha256 is not None or args.follow_up_confirmation is None
                    or args.user_agent is not None or args.refresh or args.as_of is not None
                    or args.lag_days is not None):
                raise PageviewsError(
                    "invalid_arguments", "--from-study requires a human follow-up confirmation and cannot use collection overrides."
                )
            result = derive_study_from_pinned_source(
                JsonArtifact(Path(args.from_study[0]), args.from_study[1]), baseline, current,
                follow_up_confirmation=args.follow_up_confirmation,
                include_monthly=args.monthly, methodology=methodology,
            )
        else:
            if args.confirm_sha256 is None or args.follow_up_confirmation is not None or args.as_of is None:
                raise PageviewsError(
                    "invalid_arguments", "--resolution requires --confirm-sha256 and --as-of, and does not accept follow-up confirmation."
                )
            plan = read_resolution(args.resolution, args.confirm_sha256)
            result = run_study(
                plan, baseline, current, as_of=args.as_of, lag_days=args.lag_days if args.lag_days is not None else 7,
                cache_dir=args.cache_dir, user_agent=args.user_agent, timeout=args.timeout,
                offline=args.offline, refresh=args.refresh, include_monthly=args.monthly,
                methodology=methodology,
            )
        saved = save_json_artifact(result, output)
        result["artifacts"] = {"study": str(saved.path), "study_sha256": saved.sha256}
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    summary = result["summary"]
    return 1 if summary["failed_collections"] or summary["failed_resolution_checks"] else 0