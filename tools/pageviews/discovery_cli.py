from collections.abc import Sequence
from pathlib import Path

from .artifacts import JsonArtifact
from .cli_common import JsonArgumentParser, print_error, print_result
from .discovery import (
    approve_discovery, begin_discovery, collect_discovery, discovery_summary, load_discovery_scope,
    resolve_discovery, revise_discovery, search_discovery,
)
from .errors import PageviewsError


def _ref(pair) -> JsonArtifact:
    return JsonArtifact(Path(pair[0]), pair[1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Persistent topic-independent discovery; approval is host-owned, collection defaults offline.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("begin", "revise", "show", "search", "resolve", "approve", "collect"):
        command = actions.add_parser(name)
        if name != "begin":
            command.add_argument("--state", nargs=2, required=True, metavar=("FILE", "SHA256"))
        if name != "show":
            command.add_argument("--output", type=Path, required=True, help="New state JSON; sidecars use the same stem")
        if name in ("begin", "revise"):
            command.add_argument("--question", required=True)
            command.add_argument("--criterion", action="append", default=[])
            command.add_argument("--scope", nargs=2, metavar=("FILE", "SHA256"), help="Complete explicit scope JSON; omitted means needs_scope")
        if name in ("search", "resolve", "collect"):
            command.add_argument("--user-agent", required=name != "collect")
            command.add_argument("--timeout", type=float, default=30.0)
        if name == "search":
            command.add_argument("--next-page", action="store_true")
        if name == "resolve":
            command.add_argument("--entity", required=True)
        if name == "approve":
            command.add_argument("--confirm-sha256", required=True, help="Exact discovery proposal checksum, not resolution checksum")
            command.add_argument("--user-reply", required=True, help="Host-verified human confirmation of mappings and scope")
        if name == "collect":
            command.add_argument("--online", action="store_true", help="Explicitly allow pageview requests; default uses cache only")
            command.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parents[2] / "assets" / "pageviews")
    try:
        args = parser.parse_args(argv)
        if args.action == "show":
            reference = _ref(args.state)
        elif args.action in ("begin", "revise"):
            options = {"question": args.question, "criteria": args.criterion, "output": args.output,
                       "scope": load_discovery_scope(_ref(args.scope)) if args.scope else None}
            reference = begin_discovery(**options) if args.action == "begin" else revise_discovery(_ref(args.state), **options)
        elif args.action == "approve":
            reference = approve_discovery(_ref(args.state), confirmation=args.confirm_sha256,
                                           user_reply=args.user_reply, output=args.output)
        else:
            options = {"user_agent": args.user_agent, "timeout": args.timeout, "output": args.output}
            if args.action == "search":
                reference = search_discovery(_ref(args.state), next_page=args.next_page, **options)
            elif args.action == "resolve":
                reference = resolve_discovery(_ref(args.state), entity=args.entity, **options)
            else:
                reference = collect_discovery(_ref(args.state), cache_dir=args.cache_dir, online=args.online, **options)
        result = discovery_summary(reference)
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    if result["status"] == "failed":
        return 1
    if "study_summary" in result:
        summary = result["study_summary"]
        return 1 if summary["failed_collections"] or summary["failed_resolution_checks"] else 0
    if "resolution" in result and result["resolution"]["result"]["summary"]["failed_checks"]:
        return 1
    return 0