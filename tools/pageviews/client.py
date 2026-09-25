import math
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import PageviewsError
from .models import PageviewsRequest, RawResponse

MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def validate_user_agent(value: str) -> str:
    if not value.strip() or any(not 32 <= ord(character) <= 126 for character in value):
        raise PageviewsError(
            "invalid_request", "User-Agent must be non-empty ASCII without line breaks."
        )
    return value.strip()


def fetch_response(
    request: PageviewsRequest,
    *,
    user_agent: str,
    timeout: float = 30.0,
) -> RawResponse:
    try:
        return fetch_url(request.url, user_agent=user_agent, timeout=timeout)
    except PageviewsError as error:
        if error.code == "http_error" and error.details.get("http_status") == 404:
            raise PageviewsError(
                "data_unavailable",
                "Wikimedia returned no series: 404 can mean zero views or data not "
                "loaded. It does not establish article absence.",
                details=error.details,
            ) from error
        raise


def fetch_url(
    url: str, *, user_agent: str, timeout: float = 30.0
) -> RawResponse:
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "timeout must be positive and finite.")
    http_request = Request(
        url,
        headers={
            "User-Agent": validate_user_agent(user_agent),
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(http_request, timeout=timeout) as response:
            if response.status != 200:
                raise PageviewsError(
                    "http_error",
                    "Expected HTTP 200 from Wikimedia.",
                    details={"http_status": response.status, "url": url},
                )
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        details = {"http_status": error.code, "url": url}
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after is not None:
            details["retry_after"] = retry_after
        error.close()
        if error.code == 429:
            raise PageviewsError(
                "rate_limited",
                "Wikimedia is limiting requests. Respect Retry-After before retrying.",
                details=details,
            ) from error
        raise PageviewsError(
            "http_error", "Wikimedia returned an HTTP error.", details=details
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise PageviewsError(
            "network_error",
            "Could not complete the Wikimedia request; no automatic retry was made.",
            details={"url": url, "exception_type": type(error).__name__},
        ) from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise PageviewsError(
            "response_too_large", "Response exceeds 10 MiB; request a smaller window."
        )
    return RawResponse(
        url=url,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        body=body,
    )