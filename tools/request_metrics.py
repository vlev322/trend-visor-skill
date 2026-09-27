from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from tools.pageviews.errors import PageviewsError

HTTP_CATEGORIES = ("model", "metadata", "pageviews")
CACHE_OUTCOMES = ("hits", "misses", "bypasses", "errors")
_ACTIVE_METRICS: ContextVar["RequestMetrics | None"] = ContextVar("trend_visor_request_metrics", default=None)


class RequestMetrics:
    """Synchronous per-event counters for actual HTTP attempts and cache lookups."""

    def __init__(self):
        self.http = {
            category: {
                "attempts": 0, "successes": 0, "errors": 0,
                "status_codes": {}, "response_body_bytes_read": 0,
                "response_body_bytes_measurements": 0,
                "measured_latency_ms": 0.0,
            }
            for category in HTTP_CATEGORIES
        }
        self.cache = {outcome: 0 for outcome in CACHE_OUTCOMES}

    def record_http(self, category: str, *, status_code: int | None,
                    response_body_bytes_read: int | None, latency_ms: float,
                    error_code: str | None) -> None:
        if category not in self.http:
            raise ValueError("Unknown HTTP metric category.")
        metrics = self.http[category]
        metrics["attempts"] += 1
        metrics["successes"] += error_code is None
        metrics["errors"] += error_code is not None
        if response_body_bytes_read is not None:
            metrics["response_body_bytes_read"] += response_body_bytes_read
            metrics["response_body_bytes_measurements"] += 1
        metrics["measured_latency_ms"] += latency_ms
        if status_code is not None:
            key = str(status_code)
            metrics["status_codes"][key] = metrics["status_codes"].get(key, 0) + 1

    def record_cache(self, outcome: str) -> None:
        if outcome not in self.cache:
            raise ValueError("Unknown cache outcome.")
        self.cache[outcome] += 1

    def snapshot(self) -> dict:
        http = {}
        for category, values in self.http.items():
            http[category] = {
                **values,
                "status_codes": dict(sorted(values["status_codes"].items())),
                "measured_latency_ms": round(values["measured_latency_ms"], 3),
            }
        return {"http": http, "cache": dict(self.cache)}

    def merge(self, other: dict) -> None:
        for category in HTTP_CATEGORIES:
            target = self.http[category]
            source = other["http"][category]
            for field in ("attempts", "successes", "errors", "response_body_bytes_read",
                          "response_body_bytes_measurements"):
                target[field] += source[field]
            target["measured_latency_ms"] += source["measured_latency_ms"]
            for status, count in source["status_codes"].items():
                target["status_codes"][status] = target["status_codes"].get(status, 0) + count
        for outcome in CACHE_OUTCOMES:
            self.cache[outcome] += other["cache"][outcome]


class _HttpAttempt:
    def __init__(self):
        self.status_code: int | None = None
        self.response_body_bytes_read: int | None = None
        self.error_code: str | None = None


def record_cache_event(outcome: str) -> None:
    metrics = _ACTIVE_METRICS.get()
    if metrics is not None:
        metrics.record_cache(outcome)


@contextmanager
def capture_request_metrics():
    metrics = RequestMetrics()
    token = _ACTIVE_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _ACTIVE_METRICS.reset(token)


@contextmanager
def track_http_attempt(category: str):
    if category not in HTTP_CATEGORIES:
        raise PageviewsError("invalid_request", "Unsupported HTTP metric category.")
    metrics = _ACTIVE_METRICS.get()
    attempt = _HttpAttempt()
    started = perf_counter()
    try:
        yield attempt
    except BaseException as error:
        attempt.error_code = error.code if isinstance(error, PageviewsError) else type(error).__name__
        raise
    finally:
        if metrics is not None:
            metrics.record_http(
                category,
                status_code=attempt.status_code,
                response_body_bytes_read=attempt.response_body_bytes_read,
                latency_ms=(perf_counter() - started) * 1000,
                error_code=attempt.error_code,
            )
