import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from .client import fetch_url
from .errors import PageviewsError
from .models import RawResponse

WIKIDATA_HOST = "www.wikidata.org"
WIKIPEDIA_HOST = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.wikipedia\.org")
READ_ACTIONS = {"query", "wbsearchentities", "wbgetentities", "sitematrix"}


@dataclass(frozen=True, slots=True)
class ActionResponse:
    payload: dict[str, object]
    source: RawResponse


def fetch_action(
    host: str,
    parameters: dict[str, str | int],
    *,
    user_agent: str,
    timeout: float = 30.0,
) -> ActionResponse:
    if host != WIKIDATA_HOST and not WIKIPEDIA_HOST.fullmatch(host):
        raise PageviewsError("invalid_request", "Expected a Wikimedia API host.")
    if parameters.get("action") not in READ_ACTIONS:
        raise PageviewsError("invalid_request", "Only read-only API actions are allowed.")
    query = {
        **parameters,
        "format": "json",
        "formatversion": 2,
        "maxlag": 5,
    }
    url = f"https://{host}/w/api.php?{urlencode(query)}"
    source = fetch_url(url, user_agent=user_agent, timeout=timeout, metric_category="metadata")
    try:
        payload = json.loads(source.body)
    except ValueError as error:
        raise PageviewsError(
            "invalid_response", "Action API response is not valid JSON.",
            details={"url": url},
        ) from error
    if not isinstance(payload, dict):
        raise PageviewsError("invalid_response", "Expected an Action API JSON object.")
    if "error" in payload:
        error = payload["error"]
        api_code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(api_code, str) or not api_code:
            raise PageviewsError(
                "invalid_response", "Malformed Action API error response.",
                details={"source": source.source_metadata()},
            )
        code = {
            "maxlag": "api_busy",
            "ratelimited": "rate_limited",
        }.get(api_code, "api_error")
        raise PageviewsError(
            code,
            "Wikimedia could not complete the API operation; no automatic retry was made.",
            details={"api_code": api_code, "source": source.source_metadata()},
        )
    if payload.get("warnings"):
        raise PageviewsError(
            "api_warning",
            "Wikimedia returned warnings; review the query before using this response.",
            details={"warnings": payload["warnings"], "source": source.source_metadata()},
        )
    return ActionResponse(payload=payload, source=source)