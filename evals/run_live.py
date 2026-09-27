"""Dev-only harness: run a real chat-completions model over the whitelisted
`pageviews` CLI to verify SKILL.md against a cheap/free model. Not part of the
skill itself and not imported by tests.

Usage:
    python3 evals/run_live.py <scenario>                       # start fresh
    python3 evals/run_live.py --resume <transcript.json> --reply "<text>"

The run stops (without asking anything interactively) whenever the model asks
a question instead of calling the tool, or when it hits the token budget.
Inspect the printed message, then continue with --resume/--reply.
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
ENV_PREFIX = "TREND_VISOR_LLM_"
ALLOWED_COMMANDS = ("search", "resolve", "study", "analyze", "chart", "report")
MAX_TOOL_OUTPUT_CHARS = 6000
MAX_TOKENS = 4096
REASONING_TOKEN_CAP = 1024

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
}

ADAPTER_NOTE = (
    "You are an AI agent equipped with the trend-visor skill described below. "
    "You have exactly one tool, `pageviews`, which runs one CLI subcommand per "
    "call: {\"command\": one of search/resolve/study/analyze/chart/report, "
    "\"args\": [\"--flag\", \"value\", ...]}. Use only flags documented in the "
    "skill text; never invent a Wikidata ID. A descriptive User-Agent is applied "
    "automatically; you do not need to pass --user-agent yourself. Keep reasoning "
    "brief. When you need the user's explicit yes/no before collecting pageviews, "
    "or to ask which candidate matches, do not call the tool — send a short "
    "normal assistant message in Ukrainian instead and stop."
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
    return base_url.rstrip("/"), model, api_key


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
        raise SystemExit(f"Model request failed: HTTP {error.code} {error.read().decode(errors='replace')[:2000]}")


def run_tool(command: object, args: object) -> dict:
    if command not in ALLOWED_COMMANDS:
        return {"status": "error", "error": {"code": "invalid_command", "message": f"Unknown command {command!r}"}}
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return {"status": "error", "error": {"code": "invalid_arguments", "message": "args must be a list of strings"}}
    if any(".." in item for item in args):
        return {"status": "error", "error": {"code": "invalid_arguments", "message": "Path traversal is not allowed."}}
    completed = subprocess.run(
        [sys.executable, "-m", "tools.pageviews", command, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
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


def main() -> int:
    scenario_name, resume_path, reply = _parse_args(sys.argv[1:])
    base_url, model, api_key = load_config(ROOT / ".env")
    os.environ.setdefault("TREND_VISOR_USER_AGENT", "trend-visor-eval/0.1 (dev live test; no contact)")

    if resume_path is not None:
        if not reply:
            raise SystemExit("--resume requires --reply \"<text>\"")
        transcript = json.loads(resume_path.read_text(encoding="utf-8"))
        messages = transcript["messages"]
        messages.append({"role": "user", "content": reply})
        start_turn = len(transcript["events"])
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
        start_turn = 0

    max_turns = int(os.environ.get("TREND_VISOR_EVAL_MAX_TURNS", "10"))
    status = "budget_exhausted"
    for turn in range(start_turn, start_turn + max_turns):
        payload = call_model(base_url, api_key, {
            "model": model, "messages": messages, "tools": TOOL_SCHEMA, "tool_choice": "auto",
            "temperature": 0, "max_tokens": MAX_TOKENS, "reasoning": {"max_tokens": REASONING_TOKEN_CAP},
        })
        choice = payload["choices"][0]
        message, finish_reason = choice["message"], choice["finish_reason"]
        transcript["events"].append({"turn": turn, "message": message, "finish_reason": finish_reason, "usage": payload.get("usage")})

        calls = message.get("tool_calls") or []
        if finish_reason == "tool_calls" and calls:
            call = calls[0]
            print(f"\n--- turn {turn}: tool call ---\n{call['function']['name']}({call['function']['arguments']})")
            try:
                arguments = json.loads(call["function"]["arguments"])
            except ValueError:
                arguments = {}
            result = run_tool(arguments.get("command"), arguments.get("args"))
            print(json.dumps(result, ensure_ascii=False, indent=2)[:MAX_TOOL_OUTPUT_CHARS])
            transcript["events"][-1]["tool_result"] = result
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": [call]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
            continue

        content = message.get("content") or ""
        if finish_reason == "length" and not content.strip():
            print(f"\n!!! turn {turn}: truncated before any visible content (finish_reason=length); "
                  "the model used its entire completion budget without producing a visible answer.")
            status = "truncated"
        else:
            print(f"\n=== turn {turn}: model message ===\n{content}\n")
            status = "needs_human_reply" if content.strip() else "empty_stop"
        break
    else:
        status = "budget_exhausted"

    transcript["status"] = status
    output = ROOT / "evals" / f"live-{transcript['scenario']}-{uuid4().hex}.json"
    output.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nStatus: {status}. Saved transcript to {output}")
    if status == "needs_human_reply":
        print(f'Continue with: python3 evals/run_live.py --resume {output} --reply "<your reply>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
