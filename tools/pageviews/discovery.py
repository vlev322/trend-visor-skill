import hashlib
import hmac
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .analysis import Period, validate_period_order
from .artifacts import MAX_ARTIFACT_BYTES, JsonArtifact, json_output_path, save_json_artifact
from .client import validate_user_agent
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .methodology import MethodologyOptions
from .models import EARLIEST_DATE, parse_date
from .report_sources import load_report_source
from .research_state import create_research, read_research
from .resolutions import read_resolution, save_resolution
from .studies import run_study
from .topic_data import entity_id, language_code, query_text, response_id
from .topics import resolve_topic, search_topics

DISCOVERY_VERSION = 1
MAX_STATE_BYTES = 65536
MAX_OPERATIONS = 20


def _text(value: object, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Expected bounded, single-line text.")
    value.encode("utf-8")
    return value


def _ref(value: object) -> JsonArtifact:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError("Expected an artifact reference.")
    if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
        raise ValueError("Artifact path must be absolute.")
    if not isinstance(value["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is None:
        raise ValueError("Expected a lowercase SHA256.")
    return JsonArtifact(Path(value["path"]), value["sha256"])


def _record(reference: JsonArtifact) -> dict:
    if not isinstance(reference, JsonArtifact):
        raise ValueError("Expected a pinned artifact.")
    result = {"path": str(Path(reference.path).expanduser().resolve()), "sha256": reference.sha256}
    _ref(result)
    return result


def _read_json(reference: JsonArtifact, maximum: int = MAX_STATE_BYTES) -> dict:
    ref = _ref(_record(reference))
    with ref.path.open("rb") as file:
        body = file.read(maximum + 1)
    if len(body) > maximum or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), ref.sha256):
        raise ValueError("Artifact size or checksum mismatch.")
    value = strict_json_loads(body)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _periods(scope: dict) -> tuple[Period, Period]:
    return tuple(Period(parse_date(scope[f"{name}_start"], "start"), parse_date(scope[f"{name}_end"], "end"))
                 for name in ("baseline", "current"))


def _scope(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "query", "search_language", "languages", "baseline_start", "baseline_end",
        "current_start", "current_end", "as_of", "lag_days", "methodology",
    }:
        raise ValueError("Supply all explicit discovery scope fields.")
    if query_text(value["query"]) != value["query"] or language_code(value["search_language"]) != value["search_language"]:
        raise ValueError("Use canonical query and language codes.")
    languages = value["languages"]
    if (not isinstance(languages, list) or not 1 <= len(languages) <= 50
            or any(language_code(item) != item for item in languages) or len(set(languages)) != len(languages)):
        raise ValueError("Supply 1–50 distinct canonical Wikipedia language codes.")
    baseline, current = _periods(value)
    validate_period_order(baseline, current)
    lag = value["lag_days"]
    if (baseline.start < EARLIEST_DATE or type(lag) is not int or lag < 0
            or (parse_date(value["as_of"], "as_of") - current.end).days <= lag):
        raise ValueError("Requested dates are outside the available/safe window; no clipping is applied.")
    method = value["methodology"]
    if method is not None:
        if not isinstance(method, dict) or set(method) != {"trend_model", "hac_lags"}:
            raise ValueError("Invalid methodology settings.")
        MethodologyOptions(**method)


def _shape(state: dict) -> None:
    if set(state) != {"discovery_version", "discovery_id", "revision", "question", "criteria", "scope",
                      "status", "parent", "search", "selection", "resolution", "approval", "study", "research", "error", "operations"}:
        raise ValueError("Unexpected discovery state fields.")
    if (type(state["discovery_version"]) is not int or state["discovery_version"] != DISCOVERY_VERSION
            or not isinstance(state["discovery_id"], str) or re.fullmatch(r"[a-f0-9]{32}", state["discovery_id"]) is None
            or type(state["revision"]) is not int or not 0 <= state["revision"] <= 1000000
            or type(state["operations"]) is not int or not 0 <= state["operations"] <= MAX_OPERATIONS):
        raise ValueError("Invalid discovery identity or counters.")
    _text(state["question"], 1000)
    if not isinstance(state["criteria"], list) or len(state["criteria"]) > 5:
        raise ValueError("Invalid unresolved criteria.")
    for item in state["criteria"]:
        _text(item, 200)
    if state["revision"] == 0:
        if state["parent"] is not None:
            raise ValueError("Initial state cannot have a parent.")
    else:
        _ref(state["parent"])
    status = state["status"]
    if status not in ("needs_scope", "ready_to_search", "searched", "awaiting_confirmation", "approved", "collected", "failed"):
        raise ValueError("Unknown discovery status.")
    if status == "needs_scope":
        if state["scope"] is not None:
            raise ValueError("Incomplete discovery must not contain scope.")
    else:
        _scope(state["scope"])
    for key in ("search", "resolution", "study", "research"):
        if state[key] is not None:
            _ref(state[key])
    if status in ("needs_scope", "ready_to_search") and any(state[key] is not None for key in ("search", "selection", "resolution", "approval", "study", "research", "error")):
        raise ValueError("New scope must clear previous evidence and approval.")
    if status in ("searched", "awaiting_confirmation", "approved", "collected") and state["search"] is None:
        raise ValueError("A searched state requires a search result.")
    if status in ("awaiting_confirmation", "approved", "collected"):
        response_id(state["selection"])
        _ref(state["resolution"])
    elif status != "failed" and (state["selection"] is not None or state["resolution"] is not None):
        raise ValueError("Unexpected article selection.")
    if status in ("approved", "collected"):
        approval = state["approval"]
        if not isinstance(approval, dict) or set(approval) != {"proposal", "user_reply"}:
            raise ValueError("Missing article approval.")
        _ref(approval["proposal"])
        _text(approval["user_reply"], 2000)
    elif state["approval"] is not None:
        raise ValueError("Approval must not survive an unsuccessful operation.")
    if status == "collected":
        _ref(state["study"])
        _ref(state["research"])
    elif state["study"] is not None or state["research"] is not None:
        raise ValueError("Unexpected collected output.")
    if status == "failed":
        error = state["error"]
        if not isinstance(error, dict) or set(error) != {"operation", "error"} or error["operation"] not in ("search", "resolve", "collect"):
            raise ValueError("Invalid operation failure.")
        if not isinstance(error["error"], dict) or not isinstance(error["error"].get("code"), str):
            raise ValueError("Missing operation error code.")
    elif state["error"] is not None:
        raise ValueError("Unexpected error record.")


def _search_result(state: dict) -> dict:
    result = _read_json(_ref(state["search"]), MAX_ARTIFACT_BYTES)
    scope = state["scope"]
    if (type(result["schema_version"]) is not int or result["schema_version"] != 1 or result["operation"] != "search"
            or result["query"] != scope["query"] or result["language"] != scope["search_language"]
            or type(result["offset"]) is not int or not 0 <= result["offset"] <= 10000):
        raise ValueError("Search result differs from discovery scope.")
    candidates = result["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 50:
        raise ValueError("Invalid saved candidates.")
    identifiers = []
    for row in candidates:
        identifier = response_id(row["entity_id"])
        if row["url"] != f"https://www.wikidata.org/wiki/{identifier}":
            raise ValueError("Candidate URL differs from its identity.")
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers) or result["status"] != ("candidates_found" if candidates else "no_candidates"):
        raise ValueError("Invalid candidate set or search status.")
    offset = result["next_offset"]
    if offset is not None and (type(offset) is not int or not result["offset"] < offset <= 10000):
        raise ValueError("Invalid search pagination.")
    return result


def _proposal(state: dict) -> None:
    reference = state["approval"]["proposal"]
    parent = _read_json(_ref(reference))
    _shape(parent)
    if (parent["status"] != "awaiting_confirmation" or parent["discovery_id"] != state["discovery_id"]
            or any(parent[key] != state[key] for key in ("question", "criteria", "scope", "search", "selection", "resolution"))):
        raise ValueError("Approved context differs from its exact reviewed proposal.")
    if state["status"] == "approved" and (state["parent"] != reference or state["revision"] != parent["revision"] + 1):
        raise ValueError("Approval lineage changed.")


def _verify(state: dict) -> None:
    result = _search_result(state) if state["search"] is not None else None
    if state["selection"] is not None:
        if result is None or state["selection"] not in {row["entity_id"] for row in result["candidates"]}:
            raise ValueError("Selected entity was not in the saved search page.")
    if state["resolution"] is not None:
        ref = _ref(state["resolution"])
        plan = read_resolution(ref.path, ref.sha256)
        if plan.entity["entity_id"] != state["selection"] or [row.language for row in plan.targets] != state["scope"]["languages"]:
            raise ValueError("Resolution differs from selected entity/languages.")
    if state["approval"] is not None:
        _proposal(state)
        if not any(row.status == "matched" for row in plan.targets):
            raise ValueError("No matched article to approve.")
    if state["status"] == "collected":
        approved = _read_json(_ref(state["parent"]))
        _shape(approved)
        _proposal(approved)
        if (approved["status"] != "approved" or approved["discovery_id"] != state["discovery_id"]
                or approved["revision"] + 1 != state["revision"]
                or any(approved[key] != state[key] for key in ("scope", "question", "criteria", "search", "selection", "resolution", "approval"))):
            raise ValueError("Collected state differs from the approved context.")
        study = _ref(state["study"])
        source = load_report_source(study.path, study.sha256)
        stored_study = _read_json(study, MAX_ARTIFACT_BYTES)
        expected_method = state["scope"]["methodology"]
        if expected_method is None:
            if "methodology" in stored_study:
                raise ValueError("Study methodology was not requested in the approved scope.")
        elif stored_study.get("methodology", {}).get("parameters") != expected_method:
            raise ValueError("Study methodology differs from approved scope.")
        for row in stored_study["results"]:
            if row["status"] == "analyzed":
                actual = row["analysis"].get("methodology")
                if (expected_method is None and actual is not None) or (
                    expected_method is not None and (not isinstance(actual, dict) or actual.get("parameters") != expected_method)
                ):
                    raise ValueError("Article methodology differs from approved scope.")
        baseline, current = _periods(state["scope"])
        if (source["resolution"]["sha256"] != state["resolution"]["sha256"]
                or source["as_of"] != state["scope"]["as_of"] or source["excluded_recent_days"] != state["scope"]["lag_days"]
                or source["periods"] != {"baseline": {"start": baseline.start.isoformat(), "end": baseline.end.isoformat()},
                                         "current": {"start": current.start.isoformat(), "end": current.end.isoformat()}}):
            raise ValueError("Study scope differs from approved discovery.")
        post = read_research(_ref(state["research"]))
        if (post["inputs"]["study"] != state["study"] or post["inputs"]["question"] != state["question"]
                or post["inputs"]["criteria"] != state["criteria"] or post["status"] != "descriptive"):
            raise ValueError("Post-study handoff differs from discovery.")


def read_discovery(reference: JsonArtifact) -> dict:
    try:
        state = _read_json(reference)
        _shape(state)
        _verify(state)
        return state
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError("discovery_state_error", "Discovery state is missing, changed or inconsistent.",
                             details={"exception_type": type(error).__name__}) from error


def _save(state: dict, output: Path) -> JsonArtifact:
    try:
        _shape(state)
        if len((json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()) > MAX_STATE_BYTES:
            raise ValueError("Discovery state exceeds 64 KiB.")
    except PageviewsError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError("invalid_request", "Invalid discovery inputs.", details={"exception_type": type(error).__name__}) from error
    return save_json_artifact(state, output)


def _initial(question: str, scope: dict | None, criteria) -> dict:
    if not isinstance(criteria, (tuple, list)):
        raise PageviewsError("invalid_request", "Text criteria must be a list.")
    return {"discovery_version": DISCOVERY_VERSION, "discovery_id": uuid4().hex, "revision": 0,
            "question": question, "scope": deepcopy(scope), "criteria": list(criteria),
            "status": "needs_scope" if scope is None else "ready_to_search", "operations": 0,
            **dict.fromkeys(("parent", "search", "selection", "resolution", "approval", "study", "research", "error"))}


def begin_discovery(*, question: str, output: Path, scope: dict | None = None, criteria=()) -> JsonArtifact:
    output = json_output_path(output)
    return _save(_initial(question, scope, criteria), output)


def load_discovery_scope(reference: JsonArtifact) -> dict:
    try:
        value = _read_json(reference)
        _scope(value)
        return value
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError("invalid_request", "Discovery scope is missing, changed or invalid.",
                             details={"exception_type": type(error).__name__}) from error


def revise_discovery(reference: JsonArtifact, *, question: str, output: Path, scope: dict | None = None, criteria=()) -> JsonArtifact:
    output = json_output_path(output)
    previous = read_discovery(reference)
    state = _initial(question, scope, criteria)
    state.update(discovery_id=previous["discovery_id"], revision=previous["revision"] + 1,
                 parent=_record(reference), operations=previous["operations"])
    return _save(state, output)


def _advance(reference: JsonArtifact, allowed: tuple[str, ...], *, operation: bool = False) -> dict:
    previous = read_discovery(reference)
    if previous["status"] not in allowed:
        raise PageviewsError("invalid_request", "This operation is not available in the current discovery state.")
    if operation and previous["operations"] >= MAX_OPERATIONS:
        raise PageviewsError("discovery_budget_exhausted", "Discovery operation budget exhausted; no request was made.")
    state = deepcopy(previous)
    state.update(revision=previous["revision"] + 1, parent=_record(reference))
    if operation:
        state["operations"] += 1
    return state


def _sidecar(output: Path, kind: str) -> Path:
    return json_output_path(output.with_name(f"{output.stem}-{kind}.json"))


def _network(user_agent: str, timeout: float) -> dict:
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "Timeout must be positive and finite.")
    return {"user_agent": validate_user_agent(user_agent), "timeout": timeout}


def _failed(state: dict, operation: str, error: PageviewsError, output: Path) -> JsonArtifact:
    state.update(status="failed", approval=None, error={"operation": operation, "error": error.as_dict()})
    return _save(state, output)


def search_discovery(reference: JsonArtifact, *, user_agent: str, output: Path, timeout: float = 30.0,
                     next_page: bool = False) -> JsonArtifact:
    output = json_output_path(output)
    result_path = _sidecar(output, "search")
    state = _advance(reference, ("searched",) if next_page else ("ready_to_search",), operation=True)
    options = _network(user_agent, timeout)
    offset = _search_result(state)["next_offset"] if next_page else 0
    if offset is None:
        raise PageviewsError("invalid_request", "This search has no next page.")
    try:
        result = search_topics(state["scope"]["query"], language=state["scope"]["search_language"], offset=offset, **options)
    except PageviewsError as error:
        return _failed(state, "search", error, output)
    state.update(status="searched", search=_record(save_json_artifact(result, result_path)))
    return _save(state, output)


def resolve_discovery(reference: JsonArtifact, *, entity: str, user_agent: str, output: Path,
                      timeout: float = 30.0) -> JsonArtifact:
    output = json_output_path(output)
    result_path = _sidecar(output, "resolution")
    state = _advance(reference, ("searched",), operation=True)
    identifier = entity_id(entity)
    if identifier not in {row["entity_id"] for row in _search_result(state)["candidates"]}:
        raise PageviewsError("invalid_request", "Select an entity ID from this saved search page; do not substitute another topic.")
    options = _network(user_agent, timeout)
    state["selection"] = identifier
    try:
        result = resolve_topic(identifier, state["scope"]["languages"], label_language=state["scope"]["search_language"], **options)
    except PageviewsError as error:
        return _failed(state, "resolve", error, output)
    state.update(status="awaiting_confirmation", resolution=_record(save_resolution(result, result_path)))
    return _save(state, output)


def approve_discovery(reference: JsonArtifact, *, confirmation: str, user_reply: str, output: Path) -> JsonArtifact:
    output = json_output_path(output)
    state = _advance(reference, ("awaiting_confirmation",))
    if confirmation != reference.sha256:
        raise PageviewsError("confirmation_required", "Confirm the exact discovery proposal, including articles and dates.")
    ref = _ref(state["resolution"])
    if not any(row.status == "matched" for row in read_resolution(ref.path, ref.sha256).targets):
        raise PageviewsError("invalid_request", "No matched article is available; clarify the scope instead of approving a substitution.")
    state.update(status="approved", approval={"proposal": _record(reference), "user_reply": user_reply})
    return _save(state, output)


def collect_discovery(reference: JsonArtifact, *, cache_dir: Path, output: Path, online: bool = False,
                      user_agent: str | None = None, timeout: float = 30.0) -> JsonArtifact:
    output = json_output_path(output)
    study_path, research_path = _sidecar(output, "study"), _sidecar(output, "research")
    if read_discovery(reference)["status"] != "approved":
        raise PageviewsError("confirmation_required", "Article/scope confirmation is required before collection.")
    state = _advance(reference, ("approved",), operation=True)
    if type(online) is not bool:
        raise PageviewsError("invalid_request", "online must be an explicit boolean.")
    options = _network(user_agent, timeout) if online else {"timeout": timeout}
    ref = _ref(state["resolution"])
    plan = read_resolution(ref.path, ref.sha256)
    scope = state["scope"]
    methodology = MethodologyOptions(**scope["methodology"]) if scope["methodology"] is not None else None
    try:
        result = run_study(plan, *_periods(scope), as_of=scope["as_of"], lag_days=scope["lag_days"],
                           cache_dir=cache_dir, offline=not online, methodology=methodology, **options)
    except PageviewsError as error:
        return _failed(state, "collect", error, output)
    study = save_json_artifact(result, study_path)
    post = create_research(study, question=state["question"], criteria=state["criteria"], output=research_path)
    state.update(status="collected", study=_record(study), research=_record(post))
    return _save(state, output)


def discovery_summary(reference: JsonArtifact) -> dict:
    state = read_discovery(reference)
    actions = {"needs_scope": "clarify_scope", "ready_to_search": "search", "searched": "select_entity_or_refine",
               "awaiting_confirmation": "ask_user_to_confirm_articles_and_dates", "approved": "collect", "collected": "continue_research",
               "failed": "review_error_no_automatic_retry"}
    result = {key: state[key] for key in ("discovery_id", "revision", "question", "criteria", "scope", "status", "operations", "error")}
    result.update(operation="discovery", state=_record(reference), next_action=actions[state["status"]],
              approval_is_caller_record_not_identity_proof=True,
              limits={"operations_per_lineage": MAX_OPERATIONS, "automatic_retries": 0,
                  "search_http_upper_bound": 1,
                  "resolve_http_upper_bound": 2 + len(state["scope"]["languages"]) if state["scope"] else None})
    if state["search"] is not None:
        result["search_result"] = _search_result(state)
    if state["resolution"] is not None:
        ref = _ref(state["resolution"])
        result["resolution"] = {"artifact": state["resolution"], "result": read_resolution(ref.path, ref.sha256).result}
        matched = result["resolution"]["result"]["summary"]["matched"]
        result["limits"]["online_collection_http_upper_bound"] = matched
        if state["status"] == "awaiting_confirmation" and matched == 0:
            result["next_action"] = "clarify_scope_or_mapping"
    if state["study"] is not None:
        study = _read_json(_ref(state["study"]), MAX_ARTIFACT_BYTES)
        result.update(study=state["study"], research=state["research"], study_status=study["status"], study_summary=study["summary"])
    return result