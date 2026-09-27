import hashlib
import hmac
import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .artifacts import JsonArtifact, json_output_path, save_json_artifact
from .errors import PageviewsError
from .json_codec import strict_json_loads
from .report_attachments import load_report_attachments
from .report_criteria import load_criteria_rules
from .report_sources import load_report_source
from .reports import build_report

STATE_VERSION = 1
MAX_STATE_BYTES = 65536


def _text(value: object, maximum: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Expected bounded, nonempty single-line text.")
    value.encode("utf-8")
    return value


def _reference(value: object) -> JsonArtifact:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError("Expected an exact artifact reference.")
    path, checksum = value["path"], value["sha256"]
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("Artifact paths must be absolute.")
    if not isinstance(checksum, str) or re.fullmatch(r"[a-f0-9]{64}", checksum) is None:
        raise ValueError("Expected a lowercase SHA256.")
    return JsonArtifact(Path(path), checksum)


def _record(reference: JsonArtifact) -> dict:
    if not isinstance(reference, JsonArtifact):
        raise ValueError("Expected an artifact reference.")
    value = {"path": str(Path(reference.path).expanduser().resolve()), "sha256": reference.sha256}
    _reference(value)
    return value


def _shape(state: object) -> None:
    if not isinstance(state, dict) or set(state) != {
        "state_version", "research_id", "revision", "status", "inputs", "rules", "approval", "parent",
    }:
        raise ValueError("Unexpected research state fields.")
    if (type(state["state_version"]) is not int or state["state_version"] != STATE_VERSION
            or not isinstance(state["research_id"], str) or re.fullmatch(r"[a-f0-9]{32}", state["research_id"]) is None
            or type(state["revision"]) is not int or not 0 <= state["revision"] <= 1000000):
        raise ValueError("Unsupported research state version or identity.")
    inputs = state["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != {"study", "question", "criteria", "analyses", "charts"}:
        raise ValueError("Invalid research inputs.")
    _reference(inputs["study"])
    _text(inputs["question"], 1000)
    if not isinstance(inputs["criteria"], list) or len(inputs["criteria"]) > 5:
        raise ValueError("Invalid textual criteria.")
    for value in inputs["criteria"]:
        _text(value, 200)
    for key in ("analyses", "charts"):
        if not isinstance(inputs[key], list) or len(inputs[key]) > 50:
            raise ValueError("Invalid evidence references.")
        for reference in inputs[key]:
            _reference(reference)
    if state["revision"] == 0:
        if state["parent"] is not None or state["status"] != "descriptive":
            raise ValueError("Initial state must be descriptive with no parent.")
    else:
        _reference(state["parent"])
    if state["status"] == "descriptive":
        if state["rules"] is not None or state["approval"] is not None:
            raise ValueError("Descriptive state cannot contain approved rules.")
    elif state["status"] in ("awaiting_confirmation", "approved"):
        _reference(state["rules"])
        if inputs["criteria"]:
            raise ValueError("Unstructured criteria must be resolved before proposing rules.")
        if state["status"] == "awaiting_confirmation":
            if state["approval"] is not None:
                raise ValueError("A pending proposal cannot contain an approval.")
        else:
            approval = state["approval"]
            if not isinstance(approval, dict) or set(approval) != {"proposal_sha256", "user_reply"}:
                raise ValueError("Missing approval record.")
            if approval["proposal_sha256"] != state["parent"]["sha256"]:
                raise ValueError("Approval is not bound to its proposal.")
            _text(approval["user_reply"], 2000)
    else:
        raise ValueError("Unsupported research state status.")


def _read(reference: JsonArtifact) -> dict:
    ref = _reference(_record(reference))
    with ref.path.open("rb") as file:
        body = file.read(MAX_STATE_BYTES + 1)
    if len(body) > MAX_STATE_BYTES or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), ref.sha256):
        raise ValueError("Research state checksum or size mismatch.")
    state = strict_json_loads(body)
    _shape(state)
    return state


def _verify_inputs(state: dict) -> tuple[dict, dict | None]:
    inputs = state["inputs"]
    study = _reference(inputs["study"])
    source = load_report_source(study.path, study.sha256)
    load_report_attachments(source, analysis_artifacts=tuple(_reference(ref) for ref in inputs["analyses"]),
                            chart_artifacts=tuple(_reference(ref) for ref in inputs["charts"]))
    policy = None
    if state["rules"] is not None:
        policy = load_criteria_rules(_reference(state["rules"]), study_sha256=study.sha256, question=inputs["question"])
    return source, policy


def _read_context(reference: JsonArtifact) -> dict:
    """Validate state bytes and approval linkage, without loading the evidence twice."""
    try:
        state = _read(reference)
        if state["status"] == "approved":
            proposal = _read(_reference(state["parent"]))
            if (proposal["status"] != "awaiting_confirmation" or proposal["research_id"] != state["research_id"]
                    or proposal["revision"] + 1 != state["revision"]
                    or proposal["inputs"] != state["inputs"] or proposal["rules"] != state["rules"]):
                raise ValueError("Approved context differs from the reviewed proposal.")
        return state
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError("research_state_error", "Research state is missing, changed or inconsistent.",
                             details={"exception_type": type(error).__name__}) from error


def read_research(reference: JsonArtifact) -> dict:
    """Read a pinned state and validate current evidence; an approval checks its exact proposal too."""
    state = _read_context(reference)
    _verify_inputs(state)
    return state


def _publish(state: dict, output: Path) -> JsonArtifact:
    try:
        _shape(state)
        if len((json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()) > MAX_STATE_BYTES:
            raise ValueError("Research state exceeds 64 KiB.")
        _verify_inputs(state)
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as error:
        raise PageviewsError("invalid_request", "Invalid research state input.",
                             details={"exception_type": type(error).__name__}) from error
    return save_json_artifact(state, output)


def create_research(study: JsonArtifact, *, question: str, output: Path, criteria=(),
                    analysis_artifacts=(), chart_artifacts=()) -> JsonArtifact:
    output = json_output_path(output)
    try:
        if not all(isinstance(items, (tuple, list)) for items in (criteria, analysis_artifacts, chart_artifacts)):
            raise ValueError("Research inputs must be lists.")
        inputs = {"study": _record(study), "question": question, "criteria": list(criteria),
                  "analyses": [_record(ref) for ref in analysis_artifacts], "charts": [_record(ref) for ref in chart_artifacts]}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise PageviewsError("invalid_request", "Invalid research references.") from error
    return _publish({"state_version": STATE_VERSION, "research_id": uuid4().hex, "revision": 0,
                     "status": "descriptive", "inputs": inputs, "rules": None, "approval": None, "parent": None}, output)


def _next(state: dict, reference: JsonArtifact) -> dict:
    result = deepcopy(state)
    result.update(revision=state["revision"] + 1, parent=_record(reference), approval=None)
    return result


def propose_criteria(reference: JsonArtifact, rules: JsonArtifact, *, output: Path) -> JsonArtifact:
    output = json_output_path(output)
    state = _next(read_research(reference), reference)
    try:
        state.update(status="awaiting_confirmation", rules=_record(rules))
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise PageviewsError("invalid_request", "Invalid proposed rules reference.") from error
    return _publish(state, output)


def approve_criteria(reference: JsonArtifact, *, confirmation: str, user_reply: str, output: Path) -> JsonArtifact:
    output = json_output_path(output)
    state = read_research(reference)
    if state["status"] != "awaiting_confirmation" or confirmation != reference.sha256:
        raise PageviewsError("confirmation_required", "Confirm the exact pending research state after human review.")
    state = _next(state, reference)
    state.update(status="approved", approval={"proposal_sha256": reference.sha256, "user_reply": user_reply})
    return _publish(state, output)


def revise_research(reference: JsonArtifact, *, question: str, output: Path, study: JsonArtifact | None = None,
                    criteria=(), analysis_artifacts=(), chart_artifacts=()) -> JsonArtifact:
    """Every explicit revision clears rules/approval and replaces attachment lists, even on the same study."""
    output = json_output_path(output)
    state = _next(read_research(reference), reference)
    try:
        if not all(isinstance(items, (tuple, list)) for items in (criteria, analysis_artifacts, chart_artifacts)):
            raise ValueError("Research inputs must be lists.")
        state.update(status="descriptive", rules=None)
        state["inputs"] = {"study": _record(study) if study is not None else state["inputs"]["study"],
                           "question": question, "criteria": list(criteria),
                           "analyses": [_record(ref) for ref in analysis_artifacts],
                           "charts": [_record(ref) for ref in chart_artifacts]}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise PageviewsError("invalid_request", "Invalid revised research inputs.") from error
    return _publish(state, output)


def research_summary(reference: JsonArtifact) -> dict:
    state = _read_context(reference)
    source, policy = _verify_inputs(state)
    next_action = "report_or_revise"
    if state["status"] == "awaiting_confirmation":
        next_action = "ask_user_to_confirm_exact_state"
    elif state["inputs"]["criteria"]:
        next_action = "clarify_criteria"
    return {
        "operation": "research", "state": _record(reference), "research_id": state["research_id"],
        "revision": state["revision"], "status": state["status"], "question": state["inputs"]["question"],
        "criteria": state["inputs"]["criteria"], "study": state["inputs"]["study"],
        "periods": source["periods"], "as_of": source["as_of"],
        "languages": [{key: row[key] for key in ("language", "article", "project", "resolution_status", "status")}
                      for row in source["rows"]],
        "criteria_proposal": policy["document"] if policy is not None else None,
        "attachments": {key: len(state["inputs"][key]) for key in ("analyses", "charts")},
        "next_action": next_action,
        "approval_is_caller_record_not_identity_proof": True,
    }


def research_report(reference: JsonArtifact) -> dict:
    state = _read_context(reference)
    if state["status"] == "awaiting_confirmation":
        raise PageviewsError("confirmation_required", "Pending criteria must be confirmed or explicitly discarded by revising the state.")
    inputs = state["inputs"]
    study = _reference(inputs["study"])
    report = build_report(study.path, study.sha256, question=inputs["question"], criteria=inputs["criteria"],
                          analysis_artifacts=tuple(_reference(ref) for ref in inputs["analyses"]),
                          chart_artifacts=tuple(_reference(ref) for ref in inputs["charts"]),
                          criteria_rules=_reference(state["rules"]) if state["rules"] is not None else None)
    report["research_state"] = _record(reference)
    return report