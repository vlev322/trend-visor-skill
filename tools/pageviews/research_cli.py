from collections.abc import Sequence
from pathlib import Path

from .artifacts import JsonArtifact
from .cli_common import JsonArgumentParser, print_error, print_result
from .evidence_details import KINDS, read_evidence_detail
from .errors import PageviewsError
from .research_state import (
    approve_criteria, create_research, propose_criteria, research_summary, revise_research,
)


def _ref(pair) -> JsonArtifact:
    return JsonArtifact(Path(pair[0]), pair[1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Persist offline research context and separately confirm proposed criteria.")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "show", "detail", "propose", "approve", "revise"):
        command = commands.add_parser(name)
        if name != "start":
            command.add_argument("--state", nargs=2, required=True, metavar=("FILE", "SHA256"))
        if name not in ("show", "detail"):
            command.add_argument("--output", type=Path, required=True, help="New immutable state JSON; never overwritten")
        if name in ("start", "revise"):
            command.add_argument("--study", nargs=2, required=name == "start", metavar=("FILE", "SHA256"))
            command.add_argument("--question", required=True)
            command.add_argument("--criterion", action="append", default=[])
            command.add_argument("--analysis-result", nargs=2, action="append", default=[], metavar=("FILE", "SHA256"))
            command.add_argument("--chart-result", nargs=2, action="append", default=[], metavar=("FILE", "SHA256"))
        if name == "propose":
            command.add_argument("--rules", nargs=2, required=True, metavar=("FILE", "SHA256"))
        if name == "approve":
            command.add_argument("--confirm-sha256", required=True, help="Exact pending-state checksum reviewed by the user")
            command.add_argument("--user-reply", required=True, help="Actual non-secret user confirmation; host must verify intent")
        if name == "detail":
            command.add_argument("--language", required=True)
            command.add_argument("--kind", required=True, choices=tuple(KINDS))
            command.add_argument("--start")
            command.add_argument("--end")
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int)
    try:
        args = parser.parse_args(argv)
        if args.action == "detail":
            result = read_evidence_detail(
                _ref(args.state), language=args.language, kind=args.kind,
                start=args.start, end=args.end, offset=args.offset, limit=args.limit,
            )
            print_result(result)
            return 0
        if args.action == "show":
            reference = _ref(args.state)
        elif args.action == "propose":
            reference = propose_criteria(_ref(args.state), _ref(args.rules), output=args.output)
        elif args.action == "approve":
            reference = approve_criteria(_ref(args.state), confirmation=args.confirm_sha256,
                                          user_reply=args.user_reply, output=args.output)
        else:
            options = {"question": args.question, "criteria": args.criterion, "output": args.output,
                       "analysis_artifacts": tuple(_ref(pair) for pair in args.analysis_result),
                       "chart_artifacts": tuple(_ref(pair) for pair in args.chart_result)}
            if args.action == "start":
                reference = create_research(_ref(args.study), **options)
            else:
                reference = revise_research(_ref(args.state), study=_ref(args.study) if args.study else None, **options)
        result = research_summary(reference)
    except PageviewsError as error:
        return print_error(error)
    print_result(result)
    return 0