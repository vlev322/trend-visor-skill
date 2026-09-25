import json

from tools.pageviews.models import PageviewsRequest, build_request


def make_request(**overrides: object) -> PageviewsRequest:
    values = {
        "project": "cs.wikipedia.org",
        "article": "Přerušovaný půst",
        "start": "2026-07-12",
        "end": "2026-07-14",
        "as_of": "2026-09-25",
    }
    values.update(overrides)
    return build_request(**values)


def make_item(timestamp: str, views: object, **overrides: object) -> dict[str, object]:
    item = {
        "project": "cs.wikipedia",
        "article": "Přerušovaný_půst",
        "agent": "user",
        "access": "all-access",
        "granularity": "daily",
        "timestamp": timestamp,
        "views": views,
    }
    item.update(overrides)
    return item


def encode_items(*items: object) -> bytes:
    return json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")