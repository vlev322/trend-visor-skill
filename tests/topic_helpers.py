import json

from tools.pageviews.models import RawResponse
from tools.pageviews.wikimedia_api import ActionResponse


def action_response(payload, host="www.wikidata.org"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return ActionResponse(
        payload=payload,
        source=RawResponse(
            url=f"https://{host}/w/api.php?action=test",
            fetched_at="2026-09-25T08:00:00+00:00",
            body=body,
        ),
    )


def matrix_payload():
    return {"sitematrix": {
        "count": 4,
        "0": {"code": "cs", "site": [{
            "code": "wiki", "dbname": "cswiki", "url": "https://cs.wikipedia.org",
        }]},
        "1": {"code": "pl", "site": [{
            "code": "wiki", "dbname": "plwiki", "url": "https://pl.wikipedia.org",
        }]},
        "2": {"code": "be-tarask", "site": [{
            "code": "wiki", "dbname": "be_x_oldwiki",
            "url": "https://be-tarask.wikipedia.org",
        }]},
        "3": {"code": "en", "site": [{
            "code": "wiki", "dbname": "enwiki", "url": "https://en.wikipedia.org",
        }]},
    }}


def entity_payload(sitelinks=None):
    if sitelinks is None:
        sitelinks = {"cswiki": {
            "site": "cswiki", "title": "Přerušovaný půst", "badges": [],
        }}
    return {"entities": {"Q1666254": {
        "id": "Q1666254", "type": "item", "lastrevid": 123,
        "modified": "2026-09-25T08:00:00Z",
        "labels": {"en": {"language": "en", "value": "intermittent fasting"}},
        "descriptions": {"en": {"language": "en", "value": "Synthetic description"}},
        "sitelinks": sitelinks,
    }}, "success": 1}


def page_payload(title="Přerušovaný půst", identifier="Q1666254"):
    return {"batchcomplete": True, "query": {"pages": [{
        "pageid": 1632302, "ns": 0, "title": title, "lastrevid": 456,
        "contentmodel": "wikitext", "pagelanguage": "cs",
        "pageprops": {"wikibase_item": identifier},
        "extract": "Synthetic introductory text for checking topic scope.",
    }]}}