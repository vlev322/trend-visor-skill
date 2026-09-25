import unicodedata
from urllib.parse import quote

from .errors import PageviewsError
from .topic_data import (
    WikipediaSite, marker_present, response_id, response_list,
    response_map, response_object, response_text,
)

EXCERPT_LIMIT = 600
REDIRECT_BADGES = {"Q70893996", "Q70894304"}


def article_title(value: object) -> str:
    title = response_text(value, "article title")
    if any(char in title for char in "|#") or any(
        ord(char) < 32 or ord(char) == 127 for char in title
    ):
        raise PageviewsError("invalid_response", "Invalid standalone article title.")
    return unicodedata.normalize("NFC", title)


def article_url(site: WikipediaSite, title: str) -> str:
    return f"https://{site.project}/wiki/{quote(title.replace(' ', '_'), safe='')}"


def _title_key(title: str) -> str:
    return unicodedata.normalize("NFC", title).replace("_", " ")


def _title_changes(query: dict, field: str) -> list[dict[str, object]]:
    changes = []
    for value in response_list(query.get(field, []), field):
        row = response_object(value, field)
        fragment = row.get("tofragment", "")
        if not isinstance(fragment, str):
            raise PageviewsError("invalid_response", "Invalid redirect fragment.")
        changes.append({
            "from": article_title(row.get("from")),
            "to": article_title(row.get("to")),
            "fragment": fragment or None,
        })
    return changes


def _follow_title(query: dict, requested: str) -> tuple[str, list[dict], bool]:
    title = requested
    for change in _title_changes(query, "normalized") + _title_changes(query, "converted"):
        if _title_key(change["from"]) != _title_key(title):
            raise PageviewsError("invalid_response", "Unrelated title normalization.")
        title = change["to"]
    changes = _title_changes(query, "redirects")
    redirects = {_title_key(change["from"]): change for change in changes}
    if len(redirects) != len(changes):
        raise PageviewsError("invalid_response", "Ambiguous redirect chain.")
    followed = []
    seen = set()
    while _title_key(title) in redirects:
        key = _title_key(title)
        if key in seen:
            return title, followed, True
        seen.add(key)
        change = redirects[key]
        followed.append(change)
        title = change["to"]
    if len(followed) != len(changes):
        raise PageviewsError("invalid_response", "Unrelated redirects in page response.")
    return title, followed, False


def _page_details(page: dict, site: WikipediaSite) -> dict[str, object]:
    page_id = page.get("pageid")
    revision = page.get("lastrevid")
    namespace = page.get("ns")
    if any(type(value) is not int or value <= 0 for value in (page_id, revision)):
        raise PageviewsError("invalid_response", "Missing page or revision ID.")
    if type(namespace) is not int or namespace < 0:
        raise PageviewsError("invalid_response", "Invalid page namespace.")
    response_text(page.get("contentmodel"), "page.contentmodel")
    properties = response_map(page.get("pageprops", {}), "pageprops")
    identifier = (
        response_id(properties["wikibase_item"])
        if "wikibase_item" in properties else None
    )
    excerpt = page.get("extract", "")
    if not isinstance(excerpt, str):
        raise PageviewsError("invalid_response", "Expected a plain-text article excerpt.")
    title = article_title(page.get("title"))
    return {
        "title": title, "url": article_url(site, title),
        "page_id": page_id, "revision_id": revision, "namespace": namespace,
        "wikidata_id": identifier,
        "excerpt": excerpt[:EXCERPT_LIMIT] or None,
        "excerpt_is_preview": True,
    }


def check_article(
    payload: dict[str, object],
    site: WikipediaSite,
    sitelink_title: str,
    expected_id: str,
    *,
    badges: list[str] | None = None,
) -> dict[str, object]:
    title = article_title(sitelink_title)
    if "continue" in payload:
        raise PageviewsError("invalid_response", "Page metadata response is incomplete.")
    query = response_object(payload.get("query"), "query")
    resolved_title, redirects, circular = _follow_title(query, title)
    result: dict[str, object] = {
        "status": "needs_review",
        "sitelink": {"title": title, "url": article_url(site, title), "badges": badges or []},
        "redirects": redirects,
        "page": None,
        "issues": [],
        "download_target": None,
    }
    if circular or query.get("interwiki"):
        result["issues"] = ["redirect_unresolved" if circular else "interwiki_redirect"]
        return result
    pages = response_list(query.get("pages"), "query.pages")
    if len(pages) != 1:
        raise PageviewsError("invalid_response", "Expected exactly one queried page.")
    page = response_object(pages[0], "page")
    if marker_present(page, "invalid"):
        result["issues"] = ["invalid_title"]
        return result
    if _title_key(article_title(page.get("title"))) != _title_key(resolved_title):
        raise PageviewsError("invalid_response", "Returned title is not the requested target.")
    if marker_present(page, "known"):
        result["issues"] = ["page_not_accessible_via_api"]
        return result
    if marker_present(page, "missing"):
        result["status"] = "page_missing"
        result["issues"] = ["queried_title_missing"]
        return result

    details = _page_details(page, site)
    properties = response_map(page.get("pageprops", {}), "pageprops")
    issues = []
    if details["namespace"] != 0:
        issues.append("not_article_namespace")
    if page.get("contentmodel") != "wikitext":
        issues.append("unsupported_content_model")
    if "disambiguation" in properties:
        issues.append("disambiguation")
    if any(redirect["fragment"] for redirect in redirects):
        issues.append("section_redirect")
    if marker_present(page, "redirect"):
        issues.append("redirect_unresolved")
    if redirects:
        issues.append("redirect_requires_review")
    if details["wikidata_id"] is None:
        issues.append("wikidata_link_missing")
    elif details["wikidata_id"] != expected_id:
        issues.append("entity_mismatch")
    if REDIRECT_BADGES.intersection(badges or []):
        issues.append("sitelink_redirect_badge")
    result["page"] = details
    result["issues"] = issues
    if not issues:
        result["status"] = "matched"
        result["download_target"] = {"project": site.project, "article": details["title"]}
    return result