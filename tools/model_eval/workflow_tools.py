import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from tools.pageviews.analysis_cli import main as analyze_main
from tools.pageviews.artifacts import JsonArtifact, save_json_artifact, save_markdown_artifact
from tools.pageviews.chart_cli import main as chart_main
from tools.pageviews.client import validate_user_agent
from tools.pageviews.discovery import (
    collect_discovery, discovery_summary, read_discovery, resolve_discovery, revise_discovery, search_discovery,
)
from tools.pageviews.evidence_details import read_evidence_detail
from tools.pageviews.errors import PageviewsError
from tools.pageviews.json_codec import strict_json_loads
from tools.pageviews.report_criteria import load_criteria_rules
from tools.pageviews.report_sources import load_report_source
from tools.pageviews.reports import evidence_page
from tools.pageviews.research_state import propose_criteria, read_research, research_report, research_summary, revise_research


def _ref(value: dict) -> JsonArtifact:
    return JsonArtifact(Path(value["path"]), value["sha256"])


def reference_record(kind: str, reference: JsonArtifact) -> dict:
    return {"kind": kind, "path": str(reference.path.resolve()), "sha256": reference.sha256}


FIELDS = {
    "clarify": {"operation", "question"}, "set_scope": {"operation", "scope"},
    "search": {"operation"}, "next_page": {"operation"}, "resolve": {"operation", "entity"},
    "collect": {"operation"}, "evidence": {"operation"},
    "detail": {"operation", "language", "detail_kind"},
    "analyze": {"operation", "language", "top_days", "trim_days"}, "chart": {"operation", "language"},
    "propose_rules": {"operation", "rules", "match"}, "report": {"operation"},
}


class WorkflowAdapter:
    """Model-owned choices, host-owned references, permissions, output paths and approval gates."""

    def __init__(self, kind: str, reference: JsonArtifact, directory: Path, *, allow_lookup: bool = False,
                 allow_collection: bool = False, user_agent: str | None = None, cache_dir: Path | None = None):
        if kind not in ("discovery", "research") or type(allow_lookup) is not bool or type(allow_collection) is not bool:
            raise PageviewsError("invalid_request", "Choose a state kind and explicit network permissions.")
        if allow_lookup or allow_collection:
            if not isinstance(user_agent, str):
                raise PageviewsError("invalid_request", "Network permissions require a valid user agent.")
            validate_user_agent(user_agent)
        self.kind, self.reference = kind, reference
        self.directory = directory.expanduser().resolve()
        self.allow_lookup, self.allow_collection = allow_lookup, allow_collection
        self.user_agent = user_agent
        self.cache_dir = cache_dir or Path(__file__).resolve().parents[2] / "assets" / "pageviews"
        self.actions = 0
        self.evidence_offset = 0
        self.attempts = set()
        self.terminal = None
        self.report_artifacts = None
        self.collection_handoff = None
        self.summary()

    def summary(self) -> dict:
        if self.kind == "discovery":
            result = discovery_summary(self.reference)
            if result["status"] == "collected":
                self.collection_handoff = {key: result[key] for key in ("state", "study", "study_status", "study_summary")}
                if result["study_summary"]["failed_collections"]:
                    self.terminal = "collection_incomplete"
                self.kind, self.reference = "research", _ref(result["research"])
                result = research_summary(self.reference)
        else:
            result = research_summary(self.reference)
        if self.collection_handoff is not None:
            result["collection_handoff"] = self.collection_handoff
        return {"kind": self.kind, "context": result,
                "permissions": {"lookup_http": self.allow_lookup, "pageview_http": self.allow_collection,
                                "approvals": "host_only_not_available", "missing_value_cap": "not_available"}}

    def pause_status(self) -> str | None:
        if self.terminal is not None:
            return self.terminal
        status = self.summary()["context"]["status"]
        return status if status in ("awaiting_confirmation", "failed") else None

    def allowed_operations(self) -> list[str]:
        if self.terminal is not None:
            return []
        context = self.summary()["context"]
        status = context["status"]
        if status in ("awaiting_confirmation", "failed"):
            return []
        if self.kind == "discovery":
            operations = ["clarify"]
            if status in ("needs_scope", "ready_to_search", "searched"):
                operations.append("set_scope")
            if self.allow_lookup and status == "ready_to_search":
                operations.append("search")
            if self.allow_lookup and status == "searched":
                if context["search_result"]["candidates"]:
                    operations.append("resolve")
                if context["search_result"]["next_offset"] is not None:
                    operations.append("next_page")
            if status == "approved":
                operations.append("collect")
            return operations
        operations = ["clarify"]
        if self.evidence_offset < len(context["languages"]):
            operations.append("evidence")
        operations.extend(["detail", "report"])
        if status == "descriptive":
            operations.extend(["analyze", "chart", "propose_rules"])
        return operations

    def _path(self, label: str, suffix: str = ".json") -> Path:
        return self.directory / f"action-{self.actions:03d}-{label}{suffix}"

    def _research_inputs(self):
        state = read_research(self.reference)
        study = _ref(state["inputs"]["study"])
        return state, load_report_source(study.path, study.sha256)

    def _inspect(self, operation: str, arguments: dict) -> dict:
        state, source = self._research_inputs()
        language = arguments["language"]
        if not isinstance(language, str):
            raise PageviewsError("invalid_workflow_tool", "Language must be an analyzed language in this study.")
        row = next((row for row in source["rows"] if row["language"] == language and row["status"] == "analyzed"), None)
        if row is None:
            raise PageviewsError("invalid_workflow_tool", "Only analyzed study languages can be inspected.")
        snapshot = next(item["snapshot"] for item in source["snapshots"] if item["language"] == language)
        # Existing attachments have already been verified by read_research.
        full = research_report(self.reference)
        field = "diagnostics" if operation == "analyze" else "chart"
        if any(field in item for item in full["evidence"] if item["language"] == language):
            raise PageviewsError("workflow_operation_repeated", "This language already has that attachment; revise explicitly outside this run.")
        flags = ["--snapshot", snapshot]
        for name, period in source["periods"].items():
            flags.extend([f"--{name}-start", period["start"], f"--{name}-end", period["end"]])
        if operation == "analyze":
            for name in ("top_days", "trim_days"):
                if type(arguments[name]) is not int or not 1 <= arguments[name] <= 36525:
                    raise PageviewsError("invalid_workflow_tool", "Scenario day counts must be integers between 1 and 36525.")
            flags.extend(["--diagnostics", "--top-days", str(arguments["top_days"]), "--trim-days", str(arguments["trim_days"])])
        else:
            flags.extend(["--output", str(self._path("chart", ".png"))])
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = (analyze_main if operation == "analyze" else chart_main)(flags)
        result = strict_json_loads(stream.getvalue())
        saved = save_json_artifact(result, self._path("operation-result"))
        if code:
            raise PageviewsError("workflow_operation_failed", "The core operation failed; no retry was made.",
                                 details={"result": result, "artifact": str(saved.path)})
        analyses = [_ref(item) for item in state["inputs"]["analyses"]]
        charts = [_ref(item) for item in state["inputs"]["charts"]]
        (analyses if operation == "analyze" else charts).append(saved)
        self.reference = revise_research(self.reference, question=state["inputs"]["question"], criteria=state["inputs"]["criteria"],
                                         analysis_artifacts=analyses, chart_artifacts=charts, output=self._path("state"))
        report = research_report(self.reference)
        offset = next(index for index, item in enumerate(report["evidence"]) if item["language"] == language)
        return evidence_page(report, offset=offset)

    def _propose(self, arguments: dict) -> dict:
        state = read_research(self.reference)
        inputs = state["inputs"]
        rules = arguments["rules"]
        if not isinstance(rules, list) or not rules or not all(isinstance(rule, dict) for rule in rules):
            raise PageviewsError("invalid_workflow_tool", "Supply explicit numeric rules for human review.")
        if inputs["criteria"] and [rule.get("description") for rule in rules] != inputs["criteria"]:
            raise PageviewsError("invalid_workflow_tool", "Preserve every unresolved criterion verbatim in rule descriptions and order; clarify unsupported conditions.")
        document = {"criteria_version": 1, "study_sha256": inputs["study"]["sha256"], "question": inputs["question"],
                    "rules": rules, "match": arguments["match"]}
        saved = save_json_artifact(document, self._path("rules"))
        load_criteria_rules(saved, study_sha256=inputs["study"]["sha256"], question=inputs["question"])
        # The proposed description retains the original condition for review; no evaluation or approval occurs here.
        proposed_from = self.reference
        if inputs["criteria"]:
            proposed_from = revise_research(self.reference, question=inputs["question"], criteria=[],
                                             analysis_artifacts=tuple(_ref(ref) for ref in inputs["analyses"]),
                                             chart_artifacts=tuple(_ref(ref) for ref in inputs["charts"]),
                                             output=self._path("resolved-text-state"))
        self.reference = propose_criteria(proposed_from, saved, output=self._path("proposal-state"))
        return self.summary()["context"]

    def _detail(self, arguments: dict) -> dict:
        return read_evidence_detail(
            self.reference,
            language=arguments["language"],
            kind=arguments["detail_kind"],
            start=arguments.get("start"),
            end=arguments.get("end"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit"),
        )

    def execute(self, arguments: dict) -> dict:
        operation = arguments.get("operation") if isinstance(arguments, dict) else None
        valid_fields = isinstance(operation, str) and operation in FIELDS
        if valid_fields and operation == "detail":
            required = {"operation", "language", "detail_kind"}
            optional = {"start", "end", "offset", "limit"}
            valid_fields = required <= set(arguments) and not (set(arguments) - required - optional)
        elif valid_fields and operation == "evidence":
            valid_fields = (set(arguments) == {"operation"}
                            or set(arguments) == {"operation", "offset"})
            if valid_fields and "offset" in arguments:
                valid_fields = (type(arguments["offset"]) is str
                                and arguments["offset"] == str(self.evidence_offset))
        elif valid_fields:
            valid_fields = set(arguments) == FIELDS[operation]
        if (not valid_fields or operation not in self.allowed_operations()):
            raise PageviewsError("invalid_workflow_tool", "Operation or fields are not allowed in this state; approval, paths and network overrides are host-owned.")
        cursor = self.evidence_offset if operation == "evidence" else None
        signature = (self.kind, self.reference.sha256, json.dumps(arguments, sort_keys=True, allow_nan=False), cursor)
        if signature in self.attempts:
            raise PageviewsError("workflow_operation_repeated", "Repeated operation on the same state is not retried.")
        self.attempts.add(signature)
        self.actions += 1
        if operation == "clarify":
            text = arguments["question"]
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise PageviewsError("invalid_workflow_tool", "Provide a bounded clarification question.")
            self.terminal = "needs_user_input"
            return {"status": self.terminal, "question_uk": text, "model_text_requires_review": True,
                    "active_state": reference_record(self.kind, self.reference)}
        if self.kind == "discovery":
            state = read_discovery(self.reference)
            options = {"output": self._path("state")}
            if operation == "set_scope":
                if not isinstance(arguments["scope"], dict):
                    raise PageviewsError("invalid_workflow_tool", "Scope must be a complete explicit object.")
                self.reference = revise_discovery(self.reference, question=state["question"], criteria=state["criteria"],
                                                   scope=arguments["scope"], **options)
            elif operation in ("search", "next_page"):
                self.reference = search_discovery(self.reference, user_agent=self.user_agent, next_page=operation == "next_page", **options)
            elif operation == "resolve":
                self.reference = resolve_discovery(self.reference, entity=arguments["entity"], user_agent=self.user_agent, **options)
            elif operation == "collect":
                self.reference = collect_discovery(self.reference, cache_dir=self.cache_dir, online=self.allow_collection,
                                                   user_agent=self.user_agent, **options)
            return self.summary()["context"]
        if operation == "evidence":
            report = research_report(self.reference)
            page = evidence_page(report, offset=self.evidence_offset)
            self.evidence_offset = page["offset"] + len(page["evidence"])
            return page
        if operation == "detail":
            return self._detail(arguments)
        if operation in ("analyze", "chart"):
            return self._inspect(operation, arguments)
        if operation == "propose_rules":
            return self._propose(arguments)
        report = research_report(self.reference)
        saved = save_json_artifact(report, self._path("report"))
        markdown = save_markdown_artifact(report["markdown"], self._path("report", ".md"))
        self.report_artifacts = {"report": str(saved.path), "report_sha256": saved.sha256,
                                 "markdown": str(markdown.path), "markdown_sha256": markdown.sha256}
        self.terminal = "completed"
        return {"status": "completed", "scope": "deterministic_descriptive_report_generated",
                "report_id": report["report_id"], "narrative_review_required": True,
                "artifacts": self.report_artifacts, "active_state": reference_record(self.kind, self.reference)}