"""Dev-only harness: run a real chat-completions model over the whitelisted
`pageviews` CLI to verify SKILL.md against a cheap/free model. Not part of the
skill itself and not imported by tests.

Usage:
    .venv/bin/python evals/run_live.py <scenario>              # start fresh
    .venv/bin/python evals/run_live.py --resume <transcript.json> --reply "<text>"

The run stops (without asking anything interactively) whenever the model asks
a question instead of calling the tool, or when it hits the token budget.
Inspect the printed message, then continue with --resume/--reply. A transcript
is always saved, including on a network/subprocess failure.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").is_file() else sys.executable
ENV_PREFIX = "TREND_VISOR_LLM_"
USER_AGENT_VAR = "TREND_VISOR_USER_AGENT"
ALLOWED_COMMANDS = ("search", "resolve", "study", "report")
PATH_ARGUMENT_FLAGS = {
    "--output", "--resolution", "--cache-dir", "--from-study",
    "--study", "--markdown", "--chart-dir", "--pdf", "--output-dir",
}
ASSETS_ROOT = (ROOT / "assets").resolve()
MAX_TOOL_OUTPUT_CHARS = 6000
MAX_TOKENS = 4096
# Leaves room for visible output even if the model doesn't cap its own reasoning.
DEFAULT_REASONING_TOKENS = 1024

SCENARIOS = {
    "fasting-pl-cs": (
        "Порівняй зростання інтересу до інтервального голодування в "
        "польськомовній та чеськомовній Wikipedia за останні два роки."
    ),
    "astronomy-uk": (
        "Ми думаємо додати курс з астрономії до освітнього застосунку. "
        "Чи зростає інтерес до цієї теми в україномовній Wikipedia, і наскільки "
        "цьому зростанню можна довіряти?"
    ),
    "language-learning": (
        "Ми створюємо застосунок для вивчення мов. Порівняй інтерес до вивчення "
        "англійської у вибраних нами мовних розділах та підготуй короткий звіт: "
        "які аудиторії варто дослідити наступними й чому?"
    ),
    "war-nowadays" : (
        "Чи зростає інтерес до теми війни в сучасному світі в україномовній Wikipedia?"
        "Цікавить динаміка переглядів сторінок на цю тему за період з 2021 року по 2023 рік."
    ),
    "drone-interest": (
        "Ми досліджуємо інтерес до дронів. Чи зростає інтерес до цієї теми в україномовній Wikipedia?"
        "Цікавить динаміка переглядів сторінок на цю тему за період з 2023 року по 2025 рік."
    ),
}

ADAPTER_NOTE = (
    "You are an AI agent equipped with the trend-visor skill described below. "
    "You have exactly one tool, `pageviews`, which runs one CLI subcommand per "
    "call: {\"command\": one of search/resolve/study/report, "
    "\"args\": [\"--flag\", \"value\", ...]}. Use only flags documented in the "
    "skill text; never invent a Wikidata ID or an article title — `study --article` "
    "requires the user's own exact title. A descriptive User-Agent is applied "
    "automatically; you do not need to pass --user-agent yourself. Keep reasoning "
    "brief. When you need the user's explicit yes/no before collecting pageviews, "
    "or to ask which candidate matches, or which language editions to use, do not "
    "call the tool — send a short normal assistant message in Ukrainian instead "
    "and stop."
)

TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "pageviews",
        "description": "Run one trend-visor CLI subcommand exactly as documented in the skill text.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": list(ALLOWED_COMMANDS)},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command", "args"],
            "additionalProperties": False,
        },
    },
}]


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_file: Path) -> tuple[str, str, str]:
    file_values = _load_env_file(env_file)
    config = {
        name: os.environ.get(ENV_PREFIX + name, file_values.get(ENV_PREFIX + name))
        for name in ("BASE_URL", "MODEL", "API_KEY")
    }
    base_url, model, api_key = config["BASE_URL"], config["MODEL"], config["API_KEY"]
    if not base_url or not api_key or not model:
        raise SystemExit(
            f"Set {ENV_PREFIX}BASE_URL, {ENV_PREFIX}MODEL and {ENV_PREFIX}API_KEY "
            f"in {env_file} or the environment."
        )
    user_agent = os.environ.get(USER_AGENT_VAR, file_values.get(USER_AGENT_VAR))
    if not user_agent or "no contact" in user_agent.lower():
        raise SystemExit(
            f"Set {USER_AGENT_VAR} in {env_file} or the environment to a descriptive "
            "User-Agent with your real contact info before making real Wikimedia requests."
        )
    os.environ[USER_AGENT_VAR] = user_agent
    return base_url.rstrip("/"), model, api_key


class ModelRequestError(RuntimeError):
    """The model endpoint could not be reached or returned an error response."""


def call_model(base_url: str, api_key: str, body: dict) -> dict:
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ModelRequestError(f"HTTP {error.code}: {error.read().decode(errors='replace')[:2000]}") from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ModelRequestError(f"{type(error).__name__}: {error}") from error


def _rejected_path_argument(value: str) -> str | None:
    try:
        candidate = (ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        candidate.relative_to(ASSETS_ROOT)
    except (OSError, ValueError):
        return f"Path argument must resolve inside assets/: {value!r}"
    return None


def run_tool(command: object, args: object) -> dict:
    if command not in ALLOWED_COMMANDS:
        return {"status": "error", "error": {"code": "invalid_command", "message": f"Unknown command {command!r}"}}
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return {"status": "error", "error": {"code": "invalid_arguments", "message": "args must be a list of strings"}}
    for index, item in enumerate(args):
        if item in PATH_ARGUMENT_FLAGS and index + 1 < len(args):
            problem = _rejected_path_argument(args[index + 1])
            if problem is not None:
                return {"status": "error", "error": {"code": "invalid_arguments", "message": problem}}
    try:
        completed = subprocess.run(
            [PYTHON, "-m", "tools.pageviews", command, *args],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": {"code": "cli_timeout", "message": "The CLI call exceeded 180 seconds."}}
    try:
        return json.loads(completed.stdout)
    except ValueError:
        text = (completed.stdout + completed.stderr)[-MAX_TOOL_OUTPUT_CHARS:]
        return {"status": "error", "error": {"code": "cli_output_error", "message": text}}


def _parse_args(argv: list[str]) -> tuple[str | None, Path | None, str | None]:
    scenario, resume, reply = None, None, None
    index = 0
    if argv and not argv[0].startswith("--"):
        scenario, index = argv[0], 1
    while index < len(argv):
        if argv[index] == "--resume":
            resume, index = Path(argv[index + 1]), index + 2
        elif argv[index] == "--reply":
            reply, index = argv[index + 1], index + 2
        else:
            raise SystemExit(f"Unknown argument: {argv[index]}")
    return scenario, resume, reply


def _usage_totals(transcript: dict) -> dict:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "model_requests": 0}
    for event in transcript["events"]:
        usage = event.get("usage") or {}
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        totals["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        totals["model_requests"] += 1
    return totals


def run(messages: list, transcript: dict, *, base_url: str, api_key: str, model: str) -> str:
    """Drive the conversation until the model asks a question, hits its budget,
    or fails; always returns a status instead of raising, so the caller can save
    the transcript no matter how the run ended."""
    max_turns = int(os.environ.get("TREND_VISOR_EVAL_MAX_TURNS", "10"))
    reasoning_tokens = os.environ.get("TREND_VISOR_EVAL_REASONING_TOKENS", str(DEFAULT_REASONING_TOKENS))
    request_body = {
        "model": model, "tools": TOOL_SCHEMA, "tool_choice": "auto",
        "temperature": 0, "max_tokens": MAX_TOKENS,
    }
    if reasoning_tokens:
        request_body["reasoning"] = {"max_tokens": int(reasoning_tokens)}

    for turn in range(max_turns):
        try:
            payload = call_model(base_url, api_key, {**request_body, "messages": messages})
        except ModelRequestError as error:
            transcript["events"].append({"turn": turn, "error": str(error)})
            return "model_request_failed"
        choice = payload["choices"][0]
        message, finish_reason = choice["message"], choice["finish_reason"]
        transcript["events"].append({"turn": turn, "message": message, "finish_reason": finish_reason, "usage": payload.get("usage")})

        calls = message.get("tool_calls") or []
        assistant_message = {"role": "assistant", "content": message.get("content")}
        if calls:
            assistant_message["tool_calls"] = calls
        messages.append(assistant_message)

        if calls:
            for call in calls:
                function = call.get("function", {})
                print(f"\n--- turn {turn}: tool call ---\n{function.get('name')}({function.get('arguments')})")
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except ValueError:
                    arguments = {}
                    function["arguments"] = "{}"  # keep conversation history valid for the next request
                result = run_tool(arguments.get("command"), arguments.get("args"))
                print(json.dumps(result, ensure_ascii=False, indent=2)[:MAX_TOOL_OUTPUT_CHARS])
                transcript["events"][-1].setdefault("tool_results", []).append(result)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result, ensure_ascii=False)})
            continue

        content = message.get("content") or ""
        had_visible_content = bool(content.strip())
        if not had_visible_content and (message.get("reasoning") or "").strip():
            # Some reasoning models park their real answer in "reasoning" and leave
            # "content" empty; surface it instead of losing the answer as empty_stop,
            # but flag it so a transcript review can tell this was a workaround.
            content = message["reasoning"]
            assistant_message["content"] = content
            transcript["events"][-1]["content_recovered_from_reasoning"] = True
            print(f"\n!!! turn {turn}: model left \"content\" empty; recovered visible text from \"reasoning\" instead.")
        if finish_reason == "length" and not had_visible_content:
            print(f"\n!!! turn {turn}: truncated before any visible content (finish_reason=length); "
                  "the model used its entire completion budget without producing a visible answer. "
                  "Any \"reasoning\" text printed above was cut off mid-generation and may be incomplete or looping "
                  "rather than a real conclusion.")
            return "truncated"
        print(f"\n=== turn {turn}: model message ===\n{content}\n")
        return "needs_human_reply" if content.strip() else "empty_stop"
    return "budget_exhausted"


def main() -> int:
    scenario_name, resume_path, reply = _parse_args(sys.argv[1:])
    base_url, model, api_key = load_config(ROOT / ".env")

    if resume_path is not None:
        if not reply:
            raise SystemExit("--resume requires --reply \"<text>\"")
        transcript = json.loads(resume_path.read_text(encoding="utf-8"))
        messages = transcript["messages"]
        messages.append({"role": "user", "content": reply})
    else:
        scenario_name = scenario_name or "fasting-pl-cs"
        if scenario_name not in SCENARIOS:
            raise SystemExit(f"Unknown scenario {scenario_name!r}; choose one of {sorted(SCENARIOS)}")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        messages = [
            {"role": "system", "content": ADAPTER_NOTE + "\n\n" + skill},
            {"role": "user", "content": SCENARIOS[scenario_name]},
        ]
        transcript = {"scenario": scenario_name, "model": model, "messages": messages, "events": []}

    try:
        status = run(messages, transcript, base_url=base_url, api_key=api_key, model=model)
    except Exception as error:  # noqa: BLE001 - always save the transcript, even on a bug
        status = "error"
        transcript["events"].append({"error": f"{type(error).__name__}: {error}"})
    finally:
        transcript["status"] = status
        transcript["usage_totals"] = _usage_totals(transcript)
        output = ROOT / "evals" / f"live-{transcript['scenario']}-{uuid4().hex}.json"
        output.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nStatus: {status}. Saved transcript to {output}")
        print(f"Usage: {transcript['usage_totals']}")
        if status == "needs_human_reply":
            print(f'Continue with: .venv/bin/python evals/run_live.py --resume {output} --reply "<your reply>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

