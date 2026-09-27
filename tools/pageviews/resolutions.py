import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import SCHEMA_VERSION
from .article_checks import REDIRECT_BADGES, article_title, article_url
from .artifacts import MAX_ARTIFACT_BYTES, JsonArtifact, save_json_artifact
from .errors import PageviewsError
from .models import normalize_article, normalize_project
from .topic_data import (
    WikipediaSite, language_code, response_id, response_list,
    response_object, response_text,
)
from .topics import MAX_LANGUAGES, RESOLUTION_VERSION
from .wikimedia_api import WIKIDATA_HOST

TARGET_STATUSES = {
    "matched", "no_sitelink", "needs_review", "page_missing",
    "unsupported_language", "unavailable_project", "check_failed", "not_checked",
    "user_confirmed",
}


@dataclass(frozen=True, slots=True)
class ResolutionTarget:
    language: str
    status: str
    project: str | None
    article: str | None
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    path: Path
    sha256: str
    entity: dict[str, object]
    targets: tuple[ResolutionTarget, ...]
    result: dict[str, object]


def _validate_source(value: object, host: str) -> None:
    source = response_object(value, "source")
    try:
        url = urlsplit(response_text(source.get("url"), "source.url"))
        fetched = datetime.fromisoformat(response_text(source.get("fetched_at_utc"), "fetched_at_utc"))
    except ValueError as error:
        raise PageviewsError("invalid_response", "Invalid source URL or timestamp.") from error
    if (
        url.scheme != "https" or url.netloc != host
        or url.path != "/w/api.php" or url.fragment
        or fetched.utcoffset() != timedelta(0)
        or type(source.get("http_status")) is not int or source["http_status"] != 200
        or not re.fullmatch(r"[a-f0-9]{64}", response_text(source.get("response_sha256"), "response_sha256"))
    ):
        raise PageviewsError("invalid_response", "Inconsistent source metadata.")


def _matched_article(target: dict, language: str, project: str, identifier: str) -> str:
    page = response_object(target.get("page"), "target.page")
    download = response_object(target.get("download_target"), "download_target")
    title = article_title(page.get("title"))
    supplied_title = article_title(download.get("article"))
    if (
        target.get("issues") != [] or target.get("redirects") != []
        or type(page.get("namespace")) is not int or page["namespace"] != 0
        or response_id(page.get("wikidata_id")) != identifier
        or download.get("project") != project
        or normalize_article(title) != normalize_article(supplied_title)
    ):
        raise PageviewsError("invalid_response", "Matched target has inconsistent identity.")
    for field in ("page_id", "revision_id"):
        value = page.get(field)
        if type(value) is not int or value <= 0:
            raise PageviewsError("invalid_response", f"Missing {field} in matched target.")
    sitelink = response_object(target.get("sitelink"), "sitelink")
    badges = response_list(sitelink.get("badges"), "sitelink.badges")
    if any(response_id(badge) in REDIRECT_BADGES for badge in badges):
        raise PageviewsError("invalid_response", "A redirect sitelink cannot be matched.")
    site_id = response_text(target.get("site_id"), "site_id")
    if not re.fullmatch(r"[a-z0-9_]+wiki", site_id):
        raise PageviewsError("invalid_response", "Invalid Wikipedia site ID.")
    site = WikipediaSite(language, site_id, project)
    if (
        page.get("url") != article_url(site, title)
        or sitelink.get("url") != article_url(site, article_title(sitelink.get("title")))
    ):
        raise PageviewsError("invalid_response", "Matched page URL disagrees with its title.")
    _validate_source(target.get("source"), project)
    return title


def _validate_summary(result: dict, targets: list[ResolutionTarget]) -> None:
    matched = sum(target.status == "matched" for target in targets)
    failed = sum(target.status in {"check_failed", "not_checked"} for target in targets)
    expected = {"requested": len(targets), "matched": matched, "failed_checks": failed}
    summary = response_object(result.get("summary"), "summary")
    status = "ready_for_confirmation" if matched == len(targets) else (
        "partial" if matched else "unresolved"
    )
    if result.get("status") != status or any(
        type(summary.get(key)) is not int or summary[key] != value
        for key, value in expected.items()
    ):
        raise PageviewsError("invalid_response", "Resolution summary disagrees with its targets.")


def _resolution_parts(value: object) -> tuple[dict, tuple[ResolutionTarget, ...]]:
    try:
        result = response_object(value, "resolution")
        if (
            type(result.get("schema_version")) is not int
            or result["schema_version"] != SCHEMA_VERSION
            or type(result.get("resolution_version")) is not int
            or result["resolution_version"] != RESOLUTION_VERSION
            or result.get("operation") != "resolve"
            or result.get("requires_confirmation") is not True
        ):
            raise PageviewsError("invalid_response", "Unsupported resolution format.")
        entity = response_object(result.get("entity"), "entity")
        identifier = response_id(entity.get("entity_id"))
        revision = entity.get("revision_id")
        if type(revision) is not int or revision <= 0:
            raise PageviewsError("invalid_response", "Missing entity revision ID.")
        if entity.get("url") != f"https://{WIKIDATA_HOST}/wiki/{identifier}":
            raise PageviewsError("invalid_response", "Entity URL disagrees with its ID.")
        sources = response_object(result.get("sources"), "sources")
        for field in ("site_matrix", "entity"):
            _validate_source(sources.get(field), WIKIDATA_HOST)
        rows = response_list(result.get("targets"), "targets")
        if not 1 <= len(rows) <= MAX_LANGUAGES:
            raise PageviewsError("invalid_response", "Invalid resolution target count.")
        targets = []
        seen_languages = set()
        seen_projects = set()
        for value in rows:
            target = response_object(value, "target")
            language = language_code(response_text(target.get("language"), "language"))
            status = response_text(target.get("status"), "target.status")
            if language in seen_languages or status not in TARGET_STATUSES:
                raise PageviewsError("invalid_response", "Duplicate language or unknown target status.")
            seen_languages.add(language)
            project = target.get("project")
            if project is not None:
                project = normalize_project(response_text(project, "project"))
            article = None
            if status == "matched":
                if project is None or project in seen_projects:
                    raise PageviewsError("invalid_response", "Missing or duplicate matched project.")
                seen_projects.add(project)
                article = _matched_article(target, language, project, identifier)
            elif target.get("download_target") is not None:
                raise PageviewsError("invalid_response", "Unresolved targets cannot be downloaded.")
            targets.append(ResolutionTarget(language, status, project, article, target))
        _validate_summary(result, targets)
        return entity, tuple(targets)
    except (PageviewsError, ValueError) as error:
        raise PageviewsError(
            "resolution_error", "Saved resolution is invalid or inconsistent.",
            details={"reason": str(error)},
        ) from error


def save_resolution(result: dict[str, object], path: Path) -> JsonArtifact:
    _resolution_parts(result)
    return save_json_artifact(result, path)


def read_resolution(path: Path) -> ResolutionPlan:
    try:
        path = path.expanduser().resolve()
        with path.open("rb") as file:
            body = file.read(MAX_ARTIFACT_BYTES + 1)
        if len(body) > MAX_ARTIFACT_BYTES:
            raise PageviewsError("resolution_error", "Saved resolution exceeds 10 MiB.")
        digest = hashlib.sha256(body).hexdigest()
        result = json.loads(body)
    except PageviewsError:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise PageviewsError(
            "resolution_error", "Could not read the saved resolution JSON.",
            details={"exception_type": type(error).__name__},
        ) from error
    entity, targets = _resolution_parts(result)
    return ResolutionPlan(path, digest, entity, targets, result)


def user_confirmed_plan(articles: Sequence[tuple[str, str]]) -> ResolutionPlan:
    """Build a ResolutionPlan for exact project/article titles the user has already
    confirmed themselves (e.g. Wikidata search/resolve is unavailable). Never checked
    against Wikidata: entity_id/label stay None and the report/study output marks
    these rows 'user_confirmed' instead of 'matched' so this is never mistaken for an
    independently verified match."""
    targets = []
    seen_languages = set()
    for language, title in articles:
        language = language_code(language)
        if language in seen_languages:
            raise PageviewsError("invalid_request", f"Language {language!r} given more than once.")
        seen_languages.add(language)
        project = normalize_project(f"{language}.wikipedia.org")
        article = normalize_article(title)
        record = {"status": "user_confirmed", "language": language, "project": project, "article": article}
        targets.append(ResolutionTarget(language, "user_confirmed", project, article, record))
    if not targets:
        raise PageviewsError("invalid_request", "At least one --article is required.")
    entity = {"entity_id": None, "label": None, "source": "user_confirmed"}
    return ResolutionPlan(Path("<user-confirmed>"), None, entity, tuple(targets), {"sources": []})