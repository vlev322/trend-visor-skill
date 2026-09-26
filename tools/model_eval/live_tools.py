import io
import json
from collections.abc import Sequence
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from tools.pageviews.artifacts import json_output_path, save_json_artifact
from tools.pageviews.cli import main as pageviews_main
from tools.pageviews.errors import PageviewsError
from tools.pageviews.resolutions import read_resolution

from .final_answer import ANSWER_CONTRACT_VERSION, HOST_BOUND_FIELDS, answer_facts, validate_answer
from .final_narrative import NARRATIVE_VERSION, RENDERED_FIELDS

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("pl", "cs")
PERIODS = {
    "baseline-start": "2024-09-18", "baseline-end": "2025-09-17",
    "current-start": "2025-09-18", "current-end": "2026-09-17",
}
AS_OF = "2026-09-25"
PROFILES = {
    "fasting-pl-cs": {
        "topic": "інтервальне голодування", "languages": list(LANGUAGES),
        "question": "Як змінився інтерес до інтервального голодування в польській і чеській Wikipedia?",
        "periods": PERIODS, "as_of": AS_OF,
    },
    "astronomy-uk": {
        "topic": "астрономія", "languages": ["uk"],
        "question": "Ми думаємо додати курс з астрономії до освітнього застосунку. "
        "Чи зростає інтерес до цієї теми в україномовній Wikipedia, "
        "і наскільки цьому зростанню можна довіряти?",
        "periods": {
            "baseline-start": "2024-09-19", "baseline-end": "2025-09-18",
            "current-start": "2025-09-19", "current-end": "2026-09-18",
        },
        "as_of": "2026-09-26",
    },
}
USER_AGENT = "trend-visor/0.0.1 (https://github.com/vlev322/trend-visor-skill; live evaluation)"
METHOD_FLAGS = ["--methodology", "--trend-model", "linear-calendar-hac", "--hac-lags", "7"]
FIELDS = {
    "search": {"operation", "query"}, "resolve": {"operation", "entity"},
    "study": {"operation", "mode"}, "analyze": {"operation", "language"},
    "chart": {"operation", "language"},
}


def tool_schema(phase: str, languages: Sequence[str] = LANGUAGES) -> dict:
    operations = ["search", "resolve"] if phase == "discover" else ["study", "analyze", "chart"]
    return {"type": "function", "function": {
        "name": "wikipedia_research",
        "description": "Run an allowed pageviews CLI operation in the fixed study scope. "
        "search needs query; resolve needs entity; study needs mode fresh or offline; "
        "analyze/chart need language. Paths, periods and credentials are managed by the host.",
        "parameters": {
            "type": "object", "properties": {
                "operation": {"type": "string", "enum": operations},
                "query": {"type": "string"}, "entity": {"type": "string"},
                "mode": {"type": "string", "enum": ["fresh", "offline"]},
                "language": {"type": "string", "enum": list(languages)},
            },
            "required": ["operation"], "additionalProperties": False,
        },
    }}


class LiveSession:
    def __init__(
        self, phase: str, directory: Path, *, resolution: Path | None = None,
        confirmation: str | None = None, cache_dir: Path | None = None,
        profile: str = "fasting-pl-cs",
    ) -> None:
        if phase not in {"discover", "research"}:
            raise PageviewsError("invalid_request", "Choose discover or research.")
        if profile not in PROFILES:
            raise PageviewsError("invalid_request", "Choose a supported live-test profile.")
        self.phase = phase
        self.profile = profile
        self.scope = deepcopy(PROFILES[profile])
        self.directory = json_output_path(directory / "live.json").parent
        self.cache_dir = cache_dir or ROOT / "assets" / "pageviews"
        self.results = {}
        self.attempts = set()
        self.plan = None
        if phase == "research":
            if resolution is None or confirmation is None:
                raise PageviewsError("invalid_request", "Research requires the reviewed resolution and checksum.")
            self.plan = read_resolution(resolution, confirmation)
            if [target.language for target in self.plan.targets] != self.scope["languages"]:
                raise PageviewsError("invalid_request", "Resolution languages differ from the selected test profile.")
            if not any(target.status == "matched" for target in self.plan.targets):
                raise PageviewsError("invalid_request", "No matched article is available for the live research phase.")
        elif resolution is not None or confirmation is not None:
            raise PageviewsError("invalid_request", "Discovery does not accept approval arguments.")

    def _invoke(self, name: str, arguments: list[str]) -> dict:
        if name in self.attempts:
            raise PageviewsError("live_operation_repeated", "Each live operation is allowed only once.")
        destination = json_output_path(self.directory / f"{name}-result.json")
        self.attempts.add(name)
        output = io.StringIO()
        with redirect_stdout(output):
            code = pageviews_main(arguments)
        result = json.loads(output.getvalue())
        self.results[name] = result
        save_json_artifact(result, destination)
        if code:
            raise PageviewsError(
                "live_operation_failed", "A pageviews operation failed; the live sequence stopped without retry.",
                details={"operation": name, "exit_code": code, "result": result},
            )
        return result

    def execute(self, arguments: dict) -> dict:
        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in FIELDS or set(arguments) != FIELDS[operation]:
            raise PageviewsError("invalid_live_tool", "Unexpected operation or argument fields.")
        if not all(isinstance(value, str) and value for value in arguments.values()):
            raise PageviewsError("invalid_live_tool", "Operation arguments must be nonempty strings.")
        if self.phase == "discover":
            if operation == "search":
                return self._invoke("search", [
                    "search", "--query", arguments["query"], "--language", "uk", "--limit", "5",
                    "--user-agent", USER_AGENT, "--timeout", "20",
                ])
            if operation == "resolve":
                candidates = self.results.get("search", {}).get("candidates", [])
                if arguments["entity"] not in {row["entity_id"] for row in candidates}:
                    raise PageviewsError("invalid_live_tool", "Select an ID from this run's search candidates.")
                return self._invoke("resolve", [
                    "resolve", "--entity", arguments["entity"], "--languages", *self.scope["languages"],
                    "--label-language", "uk", "--user-agent", USER_AGENT, "--timeout", "20",
                    "--output", str(self.directory / "resolution.json"),
                ])
            raise PageviewsError("confirmation_required", "Discovery cannot collect or analyze pageviews.")
        if operation == "study":
            return self._study(arguments["mode"])
        if operation in {"analyze", "chart"}:
            return self._inspect(operation, arguments["language"])
        raise PageviewsError("invalid_live_tool", "Research cannot change the confirmed topic or mapping.")

    def _period_flags(self) -> list[str]:
        return [value for key, date in self.scope["periods"].items() for value in (f"--{key}", date)]

    def _study(self, mode: str) -> dict:
        if mode not in {"fresh", "offline"}:
            raise PageviewsError("invalid_live_tool", "Study mode must be fresh or offline.")
        if mode == "offline" and "study_fresh" not in self.results:
            raise PageviewsError("invalid_live_tool", "Collect once before checking offline reuse.")
        arguments = [
            "study", "--resolution", str(self.plan.path), "--confirm-sha256", self.plan.sha256,
            *self._period_flags(), "--as-of", self.scope["as_of"], "--lag-days", "7",
            "--cache-dir", str(self.cache_dir), *METHOD_FLAGS,
            "--output", str(self.directory / f"study-{mode}.json"),
        ]
        arguments.extend(
            ["--refresh", "--user-agent", USER_AGENT, "--timeout", "20"]
            if mode == "fresh" else ["--offline"]
        )
        result = self._invoke(f"study_{mode}", arguments)
        if mode == "offline":
            first = self.results["study_fresh"]
            if result["comparison"] != first["comparison"]:
                raise PageviewsError("live_cache_mismatch", "Offline comparison differs from the fresh run.")
            for previous, current in zip(first["results"], result["results"], strict=True):
                if previous["status"] == "analyzed":
                    if not current.get("cache_hit") or any(
                        current.get(key) != previous.get(key)
                        for key in ("snapshot", "source", "analysis", "request", "artifacts")
                    ):
                        raise PageviewsError("live_cache_mismatch", "Offline reuse changed a saved analysis.")
        return result

    def _inspect(self, operation: str, language: str) -> dict:
        rows = self.results.get("study_fresh", {}).get("results", [])
        row = next((row for row in rows if row["language"] == language and row["status"] == "analyzed"), None)
        if row is None:
            raise PageviewsError("invalid_live_tool", "Only an analyzed language from this study may be inspected.")
        arguments = [operation, "--snapshot", row["snapshot"], *self._period_flags()]
        arguments.extend(
            ["--diagnostics", *METHOD_FLAGS] if operation == "analyze" else
            ["--output", str(self.directory / f"{language}-daily.png")]
        )
        return self._invoke(f"{operation}_{language}", arguments)

    def expected_facts(self) -> list[dict]:
        return answer_facts(self.results)

    def research_ready(self) -> bool:
        if self.phase != "research":
            return False
        rows = self.results.get("study_fresh", {}).get("results", [])
        languages = [row["language"] for row in rows if row["status"] == "analyzed"]
        required = {"study_fresh", "study_offline"} | {
            f"{operation}_{language}" for operation in ("analyze", "chart") for language in languages
        }
        return bool(languages) and required.issubset(self.results)

    def finish(self, content: str) -> dict:
        if not isinstance(content, str) or not content.strip():
            raise PageviewsError("live_invalid_answer", "The model returned no final text.")
        if self.phase == "discover":
            if "resolve" not in self.results:
                return {"status": "needs_user_input", "message_uk": content}
            return {"status": "awaiting_confirmation", "message_uk": content,
                    "resolution": self.results["resolve"]}
        if not self.research_ready():
            raise PageviewsError("live_incomplete_workflow", "The model stopped before completing the requested operations.")
        answer = validate_answer(content, self.results)
        return {"status": "completed", "facts_verified": True, "interpretations_verified": True,
            "narrative_review_required": True,
                "offline_reuse_verified": True, "answer_contract_version": ANSWER_CONTRACT_VERSION,
                "host_bound_fields": list(HOST_BOUND_FIELDS),
            "host_rendered_fields": list(RENDERED_FIELDS), "narrative_renderer_version": NARRATIVE_VERSION,
                "answer": answer}