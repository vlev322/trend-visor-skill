import math
from collections.abc import Sequence

from . import SCHEMA_VERSION
from .article_checks import EXCERPT_LIMIT, article_title, article_url, check_article
from .client import validate_user_agent
from .errors import PageviewsError
from .topic_data import (
    WikidataItem, WikipediaSite, entity_id, language_code, parse_candidates,
    parse_entity, parse_sites, query_text, response_id, response_list, response_object,
)
from .wikimedia_api import WIKIDATA_HOST, fetch_action

RESOLUTION_VERSION = 1
MAX_LANGUAGES = 50


def _network_options(user_agent: str, timeout: float) -> dict[str, object]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise PageviewsError("invalid_request", "timeout must be positive and finite.")
    return {"user_agent": validate_user_agent(user_agent), "timeout": timeout}


def search_topics(
    query: str,
    *,
    language: str = "en",
    limit: int = 5,
    offset: int = 0,
    user_agent: str,
    timeout: float = 30.0,
) -> dict[str, object]:
    text = query_text(query)
    language = language_code(language)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise PageviewsError("invalid_request", "limit must be an integer from 1 to 50.")
    if type(offset) is not int or not 0 <= offset <= 10000:
        raise PageviewsError("invalid_request", "offset must be an integer from 0 to 10000.")
    options = _network_options(user_agent, timeout)
    response = fetch_action(WIKIDATA_HOST, {
        "action": "wbsearchentities", "search": text,
        "language": language, "uselang": language, "type": "item",
        "limit": limit, "continue": offset,
    }, **options)
    candidates = parse_candidates(response.payload, limit)
    next_offset = response.payload.get("search-continue")
    if next_offset is not None and (
        type(next_offset) is not int or not offset < next_offset <= 10000
    ):
        raise PageviewsError("invalid_response", "Invalid search continuation offset.")
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "search",
        "status": "candidates_found" if candidates else "no_candidates",
        "query": text,
        "language": language,
        "offset": offset,
        "candidates": candidates,
        "next_offset": next_offset,
        "requires_selection": bool(candidates),
        "source": response.source.source_metadata(),
        "next_action": "select_entity_id" if candidates else "refine_query_or_language",
        "caveat": "Search order is not a confidence score. Review the entity's meaning "
        "before selecting an ID; an empty search does not prove the topic is absent.",
    }


def _languages(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not 1 <= len(values) <= MAX_LANGUAGES:
        raise PageviewsError(
            "invalid_request", f"Request between 1 and {MAX_LANGUAGES} language codes."
        )
    languages = [language_code(value) for value in values]
    if len(languages) != len(set(languages)):
        raise PageviewsError("invalid_request", "Each requested language must be unique.")
    return languages


def _check_sitelink(
    target: dict[str, object],
    item: WikidataItem,
    site: WikipediaSite,
    options: dict[str, object],
) -> None:
    try:
        link = response_object(item.sitelinks[site.site_id], "sitelink")
        if link.get("site") != site.site_id:
            raise PageviewsError("invalid_response", "Sitelink site ID mismatch.")
        title = article_title(link.get("title"))
        badges = [response_id(value) for value in response_list(link.get("badges", []), "badges")]
        target["sitelink"] = {
            "title": title, "url": article_url(site, title), "badges": badges,
        }
        response = fetch_action(site.project, {
            "action": "query", "titles": title,
            "prop": "info|pageprops|extracts", "inprop": "url",
            "ppprop": "wikibase_item|disambiguation", "redirects": 1,
            "exintro": 1, "explaintext": 1, "exchars": EXCERPT_LIMIT, "exlimit": 1,
        }, **options)
        target["source"] = response.source.source_metadata()
        target.update(check_article(
            response.payload, site, title, item.identifier, badges=badges
        ))
    except PageviewsError as error:
        target["status"] = "check_failed"
        target["error"] = error.as_dict()


def _resolve_targets(
    item: WikidataItem,
    sites: dict[str, WikipediaSite | None],
    options: dict[str, object],
) -> list[dict[str, object]]:
    targets = []
    stopped = False
    for language, site in sites.items():
        target: dict[str, object] = {
            "language": language,
            "site_id": site.site_id if site else None,
            "project": site.project if site else None,
            "download_target": None,
        }
        if site is None:
            target.update(status="unsupported_language", reason="no_wikipedia_site_in_matrix")
        elif site.state != "open":
            target.update(status="unavailable_project", reason=site.state)
        elif site.site_id not in item.sitelinks:
            target.update(status="no_sitelink", reason="selected_item_has_no_language_link")
        elif stopped:
            target.update(status="not_checked", reason="request_sequence_stopped")
        else:
            _check_sitelink(target, item, site, options)
            error = target.get("error", {})
            stopped = error.get("code") in {"rate_limited", "api_busy"} or (
                error.get("details", {}).get("http_status") in {403, 429, 503}
            )
        targets.append(target)
    return targets


def resolve_topic(
    identifier: str,
    languages: Sequence[str],
    *,
    label_language: str = "en",
    user_agent: str,
    timeout: float = 30.0,
) -> dict[str, object]:
    identifier = entity_id(identifier)
    languages = _languages(languages)
    label_language = language_code(label_language)
    options = _network_options(user_agent, timeout)
    matrix = fetch_action(WIKIDATA_HOST, {
        "action": "sitematrix", "smtype": "language", "smlimit": 5000,
        "smlangprop": "code|site", "smsiteprop": "code|dbname|url",
    }, **options)
    sites = parse_sites(matrix.payload, languages)
    parameters: dict[str, str | int] = {
        "action": "wbgetentities", "ids": identifier,
        "props": "info|labels|descriptions|sitelinks",
        "languages": "|".join(dict.fromkeys((label_language, "en"))),
        "redirects": "no",
    }
    site_ids = [site.site_id for site in sites.values() if site is not None]
    if site_ids:
        parameters["sitefilter"] = "|".join(site_ids)
    response = fetch_action(WIKIDATA_HOST, parameters, **options)
    item = parse_entity(response.payload, identifier, label_language)
    targets = _resolve_targets(item, sites, options)
    matched = sum(target["status"] == "matched" for target in targets)
    failed = sum(target["status"] in {"check_failed", "not_checked"} for target in targets)
    status = "ready_for_confirmation" if matched == len(targets) else (
        "partial" if matched else "unresolved"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "resolve",
        "resolution_version": RESOLUTION_VERSION,
        "status": status,
        "entity": item.as_dict(),
        "targets": targets,
        "summary": {"requested": len(targets), "matched": matched, "failed_checks": failed},
        "requires_confirmation": True,
        "sources": {
            "site_matrix": matrix.source.source_metadata(),
            "entity": response.source.source_metadata(),
        },
        "caveat": "Matched means a direct article is linked to the selected item, "
        "not that its scope or content is equivalent across languages. Review titles "
        "and excerpts before downloading. No sitelink does not prove no article or "
        "no audience interest. Redirect-title views are not automatically combined.",
    }