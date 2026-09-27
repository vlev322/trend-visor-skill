from collections.abc import Sequence
from pathlib import Path

from .artifacts import JsonArtifact, artifact_output_path, json_output_path, save_json_artifact, save_markdown_artifact
from .cli_common import JsonArgumentParser, print_error
from .errors import PageviewsError
from .reports import build_report, evidence_json, evidence_page


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Build a verified offline text report; stdout is a bounded evidence page.")
    parser.add_argument("--research-state", nargs=2, metavar=("FILE", "SHA256"), help="Resume exact saved research inputs; no input overrides")
    parser.add_argument("--study", type=Path, help="Saved study JSON, not an evaluation live.json")
    parser.add_argument("--study-sha256", help="Exact study checksum from its saved result")
    parser.add_argument("--question", help="Single-line user question (up to 1000 characters)")
    parser.add_argument("--criterion", action="append", default=[], help="Record a user criterion; does not create a ranking rule")
    parser.add_argument("--criteria-rules", nargs=2, metavar=("FILE", "SHA256"),
                        help="Reviewed numeric criteria rules for this study/question and their exact file checksum")
    parser.add_argument("--analysis-result", nargs=2, action="append", default=[], metavar=("FILE", "SHA256"),
                        help="Saved analyze --diagnostics JSON and its exact checksum; repeat for other languages")
    parser.add_argument("--chart-result", nargs=2, action="append", default=[], metavar=("FILE", "SHA256"),
                        help="Saved chart result JSON and its exact checksum (not the PNG checksum)")
    parser.add_argument("--output", type=Path, help="New full report JSON; omitted means no JSON file write")
    parser.add_argument("--markdown", type=Path, help="New readable .md report; omitted means no Markdown file write")
    parser.add_argument("--offset", type=int, default=0, help="Evidence row offset in requested-language order")
    parser.add_argument("--limit", type=int, default=1, help="Evidence rows per response, 1–3 (default: 1)")
    try:
        args = parser.parse_args(argv)
        output = json_output_path(args.output) if args.output is not None else None
        markdown = artifact_output_path(args.markdown, ".md") if args.markdown is not None else None
        if args.research_state is not None:
            if (any(value is not None for value in (args.study, args.study_sha256, args.question, args.criteria_rules))
                    or args.criterion or args.analysis_result or args.chart_result):
                raise PageviewsError("invalid_request", "Saved research inputs cannot be overridden; create a revised state first.")
            from .research_state import research_report

            report = research_report(JsonArtifact(Path(args.research_state[0]), args.research_state[1]))
        else:
            if args.study is None or args.study_sha256 is None or args.question is None:
                parser.error("Supply --study, --study-sha256 and --question, or --research-state.")
            report = build_report(
                args.study, args.study_sha256, question=args.question, criteria=args.criterion,
                criteria_rules=JsonArtifact(Path(args.criteria_rules[0]), args.criteria_rules[1]) if args.criteria_rules else None,
                analysis_artifacts=tuple(JsonArtifact(Path(path), sha) for path, sha in args.analysis_result),
                chart_artifacts=tuple(JsonArtifact(Path(path), sha) for path, sha in args.chart_result),
            )
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