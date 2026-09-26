import json
import logging
import math

from tools.pageviews.errors import PageviewsError

from .config import ModelConfig


def validate_options(timeout: float, max_tokens: int) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "Model timeout must be positive and finite.")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
        raise PageviewsError("invalid_request", "Model output limit must be between 1 and 4096 tokens.")


def _contains_secret(value: object, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains_secret(key, secret) or _contains_secret(item, secret) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, secret) for item in value)
    return False


def _normalise(completion, secret: str) -> dict:
    payload = completion.model_dump(mode="json")
    if _contains_secret(payload, secret):
        raise PageviewsError("model_response_error", "Sensitive credential content was rejected, not recorded.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Expected one model choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("Expected an assistant message")
    content = message.get("content")
    if content is not None and (not isinstance(content, str) or len(content) > 10000):
        raise ValueError("Expected bounded assistant text")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(json.dumps(calls)) > 10000:
        raise ValueError("Expected bounded tool calls")
    usage = payload.get("usage")
    if usage is not None:
        usage = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if any(value is not None and (type(value) is not int or value < 0) for value in usage.values()):
            raise ValueError("Invalid token usage")
    result = {
        "message": {"role": "assistant", "content": content, "tool_calls": calls},
        "finish_reason": choice.get("finish_reason"),
        "model": payload.get("model"), "id": payload.get("id"), "usage": usage,
    }
    refusal = message.get("refusal")
    if refusal is not None:
        if not isinstance(refusal, str) or len(refusal) > 10000:
            raise ValueError("Expected bounded refusal text")
        result["message"]["refusal"] = refusal
    json.dumps(result, allow_nan=False)
    return result


class ModelClient:
    def __init__(
        self, config: ModelConfig, *, timeout: float = 60.0, max_tokens: int = 1200,
        transport=None,
    ) -> None:
        validate_options(timeout, max_tokens)
        try:
            import openai
        except ImportError as error:
            raise PageviewsError(
                "missing_dependency", "Install the evaluation extra with uv sync --locked --all-extras."
            ) from error
        for name in ("openai", "httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self.config = config
        self.max_tokens = max_tokens
        self._openai = openai
        self._sdk = openai.OpenAI(
            api_key=config.api_key, base_url=config.base_url,
            organization="", project="", max_retries=0, timeout=timeout,
            http_client=openai.DefaultHttpx2Client(
                transport=transport, follow_redirects=False, trust_env=False,
            ),
        )

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self._sdk.close()

    def complete(
        self, messages: list[dict], tool: dict | None, *, response_format: dict | None = None,
    ) -> dict:
        if tool is not None and response_format is not None:
            raise PageviewsError("invalid_request", "Final structured responses cannot also request tools.")
        options = {"tools": [tool], "tool_choice": "auto"} if tool is not None else {}
        if response_format is not None:
            options["response_format"] = response_format
        try:
            completion = self._sdk.chat.completions.create(
                model=self.config.model, messages=messages,
                temperature=0, max_tokens=self.max_tokens, **options,
            )
        except self._openai.APIStatusError as error:
            raise PageviewsError(
                "model_http_error", "The model endpoint rejected the request; no retry was made.",
                details={"http_status": error.status_code},
            ) from None
        except self._openai.APIConnectionError:
            raise PageviewsError(
                "model_connection_error", "Model connection or timeout failure; no retry was made."
            ) from None
        except self._openai.APIError:
            raise PageviewsError("model_response_error", "The SDK could not read the model response.") from None
        except json.JSONDecodeError:
            raise PageviewsError("model_response_error", "The SDK could not read the model response.") from None
        try:
            return _normalise(completion, self.config.api_key)
        except (AttributeError, TypeError, ValueError, IndexError, RecursionError):
            raise PageviewsError("model_response_error", "Invalid OpenAI-compatible completion response.") from None