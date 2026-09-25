import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import quote

from .errors import PageviewsError

API_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
EARLIEST_DATE = date(2015, 7, 1)
AGENT = "user"
ACCESS = "all-access"
GRANULARITY = "daily"


def parse_date(value: str, field: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise PageviewsError("invalid_request", f"{field} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PageviewsError(
            "invalid_request", f"{field} is not a valid calendar date."
        ) from error


def normalize_project(value: str) -> str:
    project = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*\.wikipedia(?:\.org)?", project):
        raise PageviewsError(
            "invalid_request", "project must be a Wikipedia host, e.g. cs.wikipedia.org."
        )
    return project if project.endswith(".org") else f"{project}.org"


def normalize_article(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PageviewsError("invalid_request", "article contains control characters.")
    article = unicodedata.normalize("NFC", value.strip()).replace(" ", "_")
    if not article or "://" in article:
        raise PageviewsError(
            "invalid_request", "article must be a confirmed title, not a URL."
        )
    return article


@dataclass(frozen=True, slots=True)
class PageviewsRequest:
    project: str
    article: str
    start: date
    end: date
    requested_end: date
    as_of: date
    lag_days: int

    @property
    def url(self) -> str:
        article = quote(self.article, safe="")
        start = self.start.strftime("%Y%m%d00")
        end = self.end.strftime("%Y%m%d00")
        return (
            f"{API_BASE}/{self.project}/{ACCESS}/{AGENT}/"
            f"{article}/{GRANULARITY}/{start}/{end}"
        )

    @property
    def expected_days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "article": self.article,
            "agent": AGENT,
            "access": ACCESS,
            "granularity": GRANULARITY,
            "timezone": "UTC",
            "requested_window": {
                "start": self.start.isoformat(),
                "end": self.requested_end.isoformat(),
            },
            "effective_window": {
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            },
            "as_of": self.as_of.isoformat(),
            "excluded_recent_days": self.lag_days,
            "end_was_clipped": self.end != self.requested_end,
        }


def build_request(
    *,
    project: str,
    article: str,
    start: str,
    end: str,
    as_of: str,
    lag_days: int = 7,
) -> PageviewsRequest:
    start_date = parse_date(start, "start")
    requested_end = parse_date(end, "end")
    as_of_date = parse_date(as_of, "as_of")
    if start_date < EARLIEST_DATE:
        raise PageviewsError(
            "invalid_request", "This API provides pageviews starting on 2015-07-01."
        )
    if requested_end < start_date:
        raise PageviewsError("invalid_request", "end must not precede start.")
    if type(lag_days) is not int or lag_days < 0:
        raise PageviewsError(
            "invalid_request", "lag_days must be a nonnegative integer."
        )
    try:
        # The reference UTC day is excluded in addition to completed buffer days.
        cutoff = as_of_date - timedelta(days=lag_days + 1)
    except OverflowError as error:
        raise PageviewsError(
            "invalid_request", "lag_days exceeds the date range."
        ) from error
    effective_end = min(requested_end, cutoff)
    if effective_end < start_date:
        raise PageviewsError(
            "invalid_request",
            "The requested window has no days before the safety cutoff.",
            details={"latest_allowed_day": cutoff.isoformat()},
        )
    return PageviewsRequest(
        project=normalize_project(project),
        article=normalize_article(article),
        start=start_date,
        end=effective_end,
        requested_end=requested_end,
        as_of=as_of_date,
        lag_days=lag_days,
    )


@dataclass(frozen=True, slots=True)
class RawResponse:
    url: str
    fetched_at: str
    body: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def source_metadata(self) -> dict[str, object]:
        return {
            "url": self.url,
            "fetched_at_utc": self.fetched_at,
            "http_status": 200,
            "response_sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DailyViews:
    day: date
    views: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.day.isoformat(),
            "views": self.views,
            "status": "missing" if self.views is None else "observed",
        }