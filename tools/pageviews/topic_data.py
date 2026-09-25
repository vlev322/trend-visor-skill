import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import PageviewsError
from .wikimedia_api import WIKIPEDIA_HOST

LANGUAGE_CODE = re.compile(r"[a-z]{2,12}(?:-[a-z0-9]{1,12})*")
ENTITY_ID = re.compile(r"Q[1-9][0-9]*")


def language_code(value: str) -> str:
    if not isinstance(value, str):
        raise PageviewsError("invalid_request", "Language code must be text.")
    code = value.strip().lower()
    if len(code) > 64 or not LANGUAGE_CODE.fullmatch(code):
        raise PageviewsError(
            "invalid_request", "Use a language code such as uk, pl, cs or be-tarask."
        )
    return code


def entity_id(value: str) -> str:
    if not isinstance(value, str):
        raise PageviewsError("invalid_request", "Wikidata item ID must be text.")
    identifier = value.strip().upper()
    if not ENTITY_ID.fullmatch(identifier):
        raise PageviewsError("invalid_request", "Use one Wikidata item ID, e.g. Q1666254.")
    return identifier


def query_text(value: str) -> str:
    if not isinstance(value, str):
        raise PageviewsError("invalid_request", "Search query must be text.")
    text = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(text) <= 500 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise PageviewsError(
            "invalid_request", "Search text must be 1–500 characters without control characters."
        )
    return text


def response_object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise PageviewsError("invalid_response", f"Expected an object at {field}.")
    return value


def response_list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise PageviewsError("invalid_response", f"Expected a list at {field}.")
    return value


def response_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PageviewsError("invalid_response", f"Expected nonempty text at {field}.")
    return value


def response_id(value: object) -> str:
    identifier = response_text(value, "entity ID")
    if not ENTITY_ID.fullmatch(identifier):
        raise PageviewsError("invalid_response", "Expected a Wikidata item ID.")
    return identifier


def _search_term(row: dict, field: str) -> tuple[str | None, str | None]:
    display = response_object(row.get("display", {}), "candidate.display")
    if field in display:
        term = response_object(display[field], f"display.{field}")
        return (
            response_text(term.get("value"), f"display.{field}.value"),
            response_text(term.get("language"), f"display.{field}.language"),
        )
    value = row.get(field)
    return (response_text(value, field), None) if value is not None else (None, None)


def parse_candidates(payload: dict[str, object], limit: int) -> list[dict[str, object]]:
    rows = response_list(payload.get("search"), "search")
    if len(rows) > limit:
        raise PageviewsError("invalid_response", "Search response exceeds the requested limit.")
    candidates = []
    seen = set()
    for value in rows:
        row = response_object(value, "candidate")
        identifier = response_id(row.get("id"))
        if identifier in seen:
            raise PageviewsError("invalid_response", "Duplicate search candidate ID.")
        seen.add(identifier)
        label, label_language = _search_term(row, "label")
        description, description_language = _search_term(row, "description")
        match = response_object(row.get("match", {}), "candidate.match")
        candidates.append({
            "entity_id": identifier,
            "label": label,
            "label_language": label_language,
            "description": description,
            "description_language": description_language,
            "url": f"https://www.wikidata.org/wiki/{identifier}",
            "match": {
                field: response_text(match[field], f"match.{field}")
                for field in ("type", "text", "language") if field in match
            },
        })
    return candidates


def response_map(value: object, field: str) -> dict:
    # Wikibase may serialize an empty associative collection as an empty list.
    return {} if value == [] else response_object(value, field)


def marker_present(record: dict, field: str) -> bool:
    if field not in record:
        return False
    value = record[field]
    if value is True or value == "":
        return True
    if value is False:
        return False
    raise PageviewsError("invalid_response", f"Unexpected boolean marker at {field}.")


@dataclass(frozen=True, slots=True)
class WikipediaSite:
    language: str
    site_id: str
    project: str
    state: str = "open"


def _parse_site(language: str, value: dict) -> WikipediaSite:
    try:
        address = urlsplit(response_text(value.get("url"), "site.url"))
    except ValueError as error:
        raise PageviewsError("invalid_response", "Invalid SiteMatrix URL.") from error
    host = address.hostname or ""
    if (
        address.scheme != "https" or address.netloc != host
        or not WIKIPEDIA_HOST.fullmatch(host) or address.path not in ("", "/")
        or address.query or address.fragment
    ):
        raise PageviewsError("invalid_response", "SiteMatrix returned an unsafe Wikipedia URL.")
    identifier = response_text(value.get("dbname"), "site.dbname")
    if not re.fullmatch(r"[a-z0-9_]+wiki", identifier):
        raise PageviewsError("invalid_response", "Invalid Wikipedia database ID.")
    state = "open"
    if marker_present(value, "closed"):
        state = "closed"
    if marker_present(value, "private"):
        state = "private"
    return WikipediaSite(language, identifier, host, state)


def parse_sites(
    payload: dict[str, object], languages: list[str]
) -> dict[str, WikipediaSite | None]:
    if "continue" in payload:
        raise PageviewsError("invalid_response", "SiteMatrix response is incomplete.")
    matrix = response_object(payload.get("sitematrix"), "sitematrix")
    count = matrix.get("count")
    if type(count) is not int or count < 0:
        raise PageviewsError("invalid_response", "Missing SiteMatrix count.")
    groups = {key: value for key, value in matrix.items() if key not in {"count", "specials"}}
    if count > 0 and not groups:
        raise PageviewsError("invalid_response", "SiteMatrix contains no language groups.")
    result: dict[str, WikipediaSite | None] = dict.fromkeys(languages)
    seen = set()
    for value in groups.values():
        group = response_object(value, "language group")
        language = response_text(group.get("code"), "language.code")
        if language not in result:
            continue
        if language in seen:
            raise PageviewsError("invalid_response", "Duplicate SiteMatrix language.")
        seen.add(language)
        sites = response_list(group.get("site"), "language.site")
        matches = []
        for value in sites:
            site = response_object(value, "site")
            if site.get("code") == "wiki":
                matches.append(site)
        if len(matches) > 1:
            raise PageviewsError("invalid_response", "Multiple Wikipedia sites for one language.")
        if matches:
            result[language] = _parse_site(language, matches[0])
    return result


@dataclass(frozen=True, slots=True)
class WikidataItem:
    identifier: str
    revision_id: int
    label: str | None
    label_language: str | None
    description: str | None
    description_language: str | None
    sitelinks: dict

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.identifier,
            "url": f"https://www.wikidata.org/wiki/{self.identifier}",
            "revision_id": self.revision_id,
            "label": self.label,
            "label_language": self.label_language,
            "description": self.description,
            "description_language": self.description_language,
        }


def _preferred_term(values: dict, language: str) -> tuple[str | None, str | None]:
    for code in dict.fromkeys((language, "en")):
        if code in values:
            term = response_object(values[code], "entity term")
            return (
                response_text(term.get("value"), "term.value"),
                response_text(term.get("language"), "term.language"),
            )
    return None, None


def parse_entity(
    payload: dict[str, object], requested_id: str, language: str
) -> WikidataItem:
    entities = response_object(payload.get("entities"), "entities")
    entity = response_object(entities.get(requested_id), "requested entity")
    if marker_present(entity, "missing"):
        raise PageviewsError(
            "entity_unavailable",
            "Wikidata did not return this item. It may be missing or a redirect; "
            "automatic item redirects are disabled. Search and choose an ID again.",
            details={"entity_id": requested_id},
        )
    if response_id(entity.get("id")) != requested_id or entity.get("type") != "item":
        raise PageviewsError("invalid_response", "Wikidata returned a different entity.")
    revision = entity.get("lastrevid")
    if type(revision) is not int or revision <= 0:
        raise PageviewsError("invalid_response", "Missing Wikidata revision ID.")
    label, label_language = _preferred_term(
        response_map(entity.get("labels"), "labels"), language
    )
    description, description_language = _preferred_term(
        response_map(entity.get("descriptions"), "descriptions"), language
    )
    return WikidataItem(
        identifier=requested_id, revision_id=revision,
        label=label, label_language=label_language,
        description=description, description_language=description_language,
        sitelinks=response_map(entity.get("sitelinks"), "sitelinks"),
    )