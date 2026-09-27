import json
from tools.pageviews.errors import PageviewsError

METHOD = "utf8_payload_bytes_upper_bound"
METHOD_VERSION = 1
SOFT_GUIDELINE_TOKENS = 10000
DEFAULT_MARGIN_PERCENT = 5
MIN_MARGIN_TOKENS = 256
MAX_CONTEXT_SOURCE_LENGTH = 1000
MAX_MODEL_ID_LENGTH = 200


def default_margin(context_window_tokens: int) -> int:
    """Reserve 5% of declared capacity, with a 256-token minimum."""
    five_percent_ceiling = (context_window_tokens * DEFAULT_MARGIN_PERCENT + 99) // 100
    return max(MIN_MARGIN_TOKENS, five_percent_ceiling)


def estimate_request_context(
    messages: list[dict], tool: dict | None, *, model: str | None,
    output_reserve_tokens: int, context_window_tokens: int | None = None,
    context_source: str | None = None, margin_tokens: int | None = None,
) -> dict:
    """Measure the complete visible request; this is not a provider tokenizer count.

    UTF-8 bytes are used as a conservative token upper estimate only for tokenizers
    with byte fallback. Provider-added framing and tokenizer behavior remain explicit
    assumptions; the caller must supply a checked model-specific capacity and source
    before a live run.
    """
    if not isinstance(messages, list) or (tool is not None and not isinstance(tool, dict)):
        raise PageviewsError("invalid_request", "Context preflight requires messages and an optional tool schema.")
    if type(output_reserve_tokens) is not int or output_reserve_tokens < 1:
        raise PageviewsError("invalid_request", "Output reserve must be a positive integer token count.")
    if model is not None and (
        not isinstance(model, str) or not 1 <= len(model) <= MAX_MODEL_ID_LENGTH
        or any(not 33 <= ord(char) <= 126 for char in model)
    ):
        raise PageviewsError("invalid_request", "Model ID must be bounded printable ASCII text.")
    if context_window_tokens is not None and (
        type(context_window_tokens) is not int or context_window_tokens < 1
    ):
        raise PageviewsError("invalid_request", "Context window must be a positive integer token count.")
    if context_source is not None and (
        not isinstance(context_source, str) or not context_source.strip()
        or len(context_source) > MAX_CONTEXT_SOURCE_LENGTH
        or any(ord(char) < 32 for char in context_source)
    ):
        raise PageviewsError("invalid_request", "Context source must be bounded, nonempty, single-line text.")
    if (context_window_tokens is None) != (context_source is None):
        raise PageviewsError("invalid_request", "A declared context window and its checked source must be supplied together.")
    if margin_tokens is not None and (type(margin_tokens) is not int or margin_tokens < MIN_MARGIN_TOKENS):
        raise PageviewsError(
            "invalid_request", f"Context margin must be an integer of at least {MIN_MARGIN_TOKENS} tokens."
        )
    if context_window_tokens is None and margin_tokens is not None:
        raise PageviewsError("invalid_request", "A context margin requires a declared context window.")

    model_value = model if model is not None else "x" * MAX_MODEL_ID_LENGTH
    payload = {
        "model": model_value,
        "messages": messages,
        "temperature": 0,
        "max_tokens": output_reserve_tokens,
    }
    if tool is not None:
        payload.update(tools=[tool], tool_choice="auto")
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PageviewsError(
            "invalid_request", "The complete model request is not valid UTF-8 JSON.",
            details={"exception_type": type(error).__name__},
        ) from error

    input_bytes = len(encoded)
    result = {
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "status": "capacity_unverified",
        "model": model,
        "model_id_included": model is not None,
        "model_id_reserve_bytes": 0 if model is not None else MAX_MODEL_ID_LENGTH,
        "input_utf8_bytes": input_bytes,
        "input_token_upper_estimate": input_bytes,
        "output_reserve_tokens": output_reserve_tokens,
        "margin_tokens": None,
        "estimated_total_tokens": None,
        "context_window_tokens": None,
        "context_source": None,
        "soft_guideline_tokens": SOFT_GUIDELINE_TOKENS,
        "soft_guideline_exceeded": input_bytes > SOFT_GUIDELINE_TOKENS,
        "assumptions": [
            "UTF-8 byte count is an upper estimate only for tokenizers with byte fallback; it is not a tokenizer measurement.",
            "Provider-added hidden instructions/framing are unknown; no 150000-token capacity is inferred from local files.",
            "The 10000-token guideline is soft and never rejects a request by itself.",
        ],
    }
    if model is None:
        result["assumptions"].append(
            "The configured model ID was not read; byte count reserves its maximum allowed 200 printable ASCII characters."
        )
    if context_window_tokens is None:
        return result

    margin = margin_tokens if margin_tokens is not None else default_margin(context_window_tokens)
    estimated_total = input_bytes + output_reserve_tokens + margin
    result.update(
        status=("fits_declared_capacity_estimate" if estimated_total <= context_window_tokens
                else "exceeds_declared_capacity_estimate"),
        margin_tokens=margin,
        estimated_total_tokens=estimated_total,
        context_window_tokens=context_window_tokens,
        context_source=context_source,
    )
    return result
