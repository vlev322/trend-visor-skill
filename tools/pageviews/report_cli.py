from collections.abc import Sequence
from pathlib import Path

from .artifacts import artifact_output_path, json_output_path, save_json_artifact, save_markdown_artifact
from .cli_common import JsonArgumentParser, print_error
from .errors import PageviewsError
from .reports import build_report, evidence_json, evidence_page


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Build a verified offline text report; stdout is a bounded evidence page.")
    parser.add_argument("--study", type=Path, required=True, help="Saved study JSON, not an evaluation live.json")
    parser.add_argument("--study-sha256", required=True, help="Exact study checksum from its saved result")
    parser.add_argument("--question", required=True, help="Single-line user question (up to 1000 characters)")
    parser.add_argument("--criterion", action="append", default=[], help="Record a user criterion; does not create a ranking rule")
    parser.add_argument("--output", type=Path, help="New full report JSON; omitted means no JSON file write")
    parser.add_argument("--markdown", type=Path, help="New readable .md report; omitted means no Markdown file write")
    parser.add_argument("--offset", type=int, default=0, help="Evidence row offset in requested-language order")
    parser.add_argument("--limit", type=int, default=1, help="Evidence rows per response, 1–3 (default: 1)")
    try:
        args = parser.parse_args(argv)
        output = json_output_path(args.output) if args.output is not None else None
        markdown = artifact_output_path(args.markdown, ".md") if args.markdown is not None else None
        report = build_report(args.study, args.study_sha256, question=args.question, criteria=args.criterion)
        result = evidence_page(report, offset=args.offset, limit=args.limit)
        result["artifacts"] = {}
        if output is not None:
            result["artifacts"].update(report=str(output), report_sha256="0" * 64)
        if markdown is not None:
            result["artifacts"].update(markdown=str(markdown), markdown_sha256="0" * 64)
        # Actual SHA256 values have the same encoded length as these placeholders.
        evidence_json(result)
        if output is not None:
            saved = save_json_artifact(report, output)
            result["artifacts"].update(report=str(saved.path), report_sha256=saved.sha256)
        if markdown is not None:
            saved = save_markdown_artifact(report["markdown"], markdown)
            result["artifacts"].update(markdown=str(saved.path), markdown_sha256=saved.sha256)
    except PageviewsError as error:
        return print_error(error)
    print(evidence_json(result), end="")
    return 0