from collections.abc import Sequence
from os import environ
from pathlib import Path

from .artifacts import json_output_path
from .cli_common import JsonArgumentParser, print_error, print_result
from .errors import PageviewsError
from .resolutions import save_resolution
from .topics import resolve_topic, search_topics


def _parser(operation: str) -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog=f"python3 -m tools.pageviews {operation}",
        description=(
            "Find Wikidata item candidates; selection remains explicit."
            if operation == "search" else
            "Check a selected Wikidata item's Wikipedia articles by language."
        ),
    )
    parser.add_argument(
        "--user-agent",
        help="Descriptive Wikimedia client identifier with real contact; defaults to TREND_VISOR_USER_AGENT",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout per request in seconds"
    )
    if operation == "search":
        parser.add_argument("--query", required=True, help="Topic text, not a guessed page title")
        parser.add_argument(
            "--language", default="en", help="Search and display language (default: en)"
        )
        parser.add_argument("--limit", type=int, default=5, help="Candidates per page: 1–50")
        parser.add_argument(
            "--offset", type=int, default=0, help="Use next_offset from an earlier search"
        )
    else:
        parser.add_argument("--entity", required=True, help="Selected Wikidata item ID, Q…")
        parser.add_argument(
            "--languages", nargs="+", required=True,
            help="Language codes separated by spaces, e.g. pl cs; not country codes",
        )
        parser.add_argument(
            "--label-language", default="en",
            help="Preferred item label/description language, with explicit English fallback",
        )
        parser.add_argument(
            "--output", type=Path,
            help="Save the resolution to a new JSON file for review before study",
        )
    return parser


def main(operation: str, argv: Sequence[str] | None = None) -> int:
    try:
        if operation not in {"search", "resolve"}:
            raise PageviewsError("invalid_arguments", "Expected search or resolve.")
        args = _parser(operation).parse_args(argv)
        user_agent = args.user_agent or environ.get("TREND_VISOR_USER_AGENT")
        if operation == "search":
            result = search_topics(
                args.query, language=args.language, limit=args.limit, offset=args.offset,
                user_agent=user_agent, timeout=args.timeout,
            )
        else:
            output = json_output_path(args.output) if args.output is not None else None
            result = resolve_topic(
                args.entity, args.languages, label_language=args.label_language,
                user_agent=user_agent, timeout=args.timeout,
            )
            if output is not None:
                saved = save_resolution(result, output)
                result["artifacts"] = {
                    "resolution": str(saved.path), "resolution_sha256": saved.sha256,
                }
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    if operation == "resolve" and result["summary"]["failed_checks"]:
        return 1
    return 0