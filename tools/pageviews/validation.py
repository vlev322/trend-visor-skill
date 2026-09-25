import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import unquote

from .errors import PageviewsError
from .models import ACCESS, AGENT, GRANULARITY, DailyViews, PageviewsRequest


@dataclass(frozen=True, slots=True)
class ValidatedSeries:
    days: tuple[DailyViews, ...]

    @property
    def status(self) -> str:
        observed = sum(day.views is not None for day in self.days)
        if observed == 0:
            return "no_observations"
        return "complete" if observed == len(self.days) else "partial"

    def coverage_summary(self) -> dict[str, object]:
        missing = [day.day.isoformat() for day in self.days if day.views is None]
        return {
            "expected_days": len(self.days),
            "observed_days": len(self.days) - len(missing),
            "missing_days": len(missing),
            "missing_dates": missing[:10],
            "missing_dates_truncated": len(missing) > 10,
            "explicit_zero_days": sum(day.views == 0 for day in self.days),
        }


def _invalid_row(index: int, reason: str, message: str) -> PageviewsError:
    return PageviewsError(
        "invalid_response", message, details={"row": index, "reason": reason}
    )


def _article_matches(value: object, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    candidates = (value, unquote(value))
    return any(
        unicodedata.normalize("NFC", title).replace(" ", "_") == expected
        for title in candidates
    )


def _validate_dimensions(
    item: dict[str, object], request: PageviewsRequest, index: int
) -> None:
    expected = {"agent": AGENT, "access": ACCESS, "granularity": GRANULARITY}
    for field, value in expected.items():
        if item.get(field) != value:
            raise _invalid_row(
                index, "wrong_dimensions", f"Unexpected {field} in response."
            )
    if item.get("project") not in (
        request.project,
        request.project.removesuffix(".org"),
    ):
        raise _invalid_row(index, "wrong_dimensions", "Unexpected project in response.")
    if not _article_matches(item.get("article"), request.article):
        raise _invalid_row(index, "wrong_dimensions", "Unexpected article in response.")


def _parse_timestamp(value: object, index: int) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}00", value):
        raise _invalid_row(index, "invalid_timestamp", "Expected a daily UTC timestamp.")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as error:
        raise _invalid_row(index, "invalid_timestamp", "Invalid calendar date.") from error


def validate_response(body: bytes, request: PageviewsRequest) -> ValidatedSeries:
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise PageviewsError(
            "invalid_response", "The response is not valid JSON."
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PageviewsError(
            "invalid_response", "Expected a JSON object with an items array."
        )

    observed: dict[date, int] = {}
    for index, item in enumerate(payload["items"], start=1):
        if not isinstance(item, dict):
            raise _invalid_row(index, "invalid_item", "Expected an object for each item.")
        _validate_dimensions(item, request, index)
        day = _parse_timestamp(item.get("timestamp"), index)
        views = item.get("views")
        if type(views) is not int or views < 0:
            raise _invalid_row(
                index, "invalid_views", "views must be a nonnegative integer."
            )
        if not request.start <= day <= request.end:
            raise _invalid_row(
                index, "out_of_range", "A date is outside the requested window."
            )
        if day in observed:
            raise _invalid_row(
                index, "duplicate_date", "A daily timestamp appears more than once."
            )
        observed[day] = views

    days = []
    for offset in range(request.expected_days):
        day = request.start + timedelta(days=offset)
        days.append(DailyViews(day=day, views=observed.get(day)))
    return ValidatedSeries(days=tuple(days))