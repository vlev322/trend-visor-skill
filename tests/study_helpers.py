import io
from unittest.mock import patch

from tests.helpers import encode_items, make_item
from tests.topic_helpers import (
    action_response, entity_payload, matrix_payload, page_payload,
)
from tools.pageviews.topics import resolve_topic

TITLES = {"cs": "Czech test article", "pl": "Polish test article", "en": "English test article"}


def resolution_result(languages=("cs", "pl"), unmatched=()):
    links = {
        f"{language}wiki": {
            "site": f"{language}wiki", "title": TITLES[language], "badges": [],
        }
        for language in languages if language not in unmatched
    }
    responses = [action_response(matrix_payload()), action_response(entity_payload(links))]
    responses.extend(
        action_response(page_payload(TITLES[language]), f"{language}.wikipedia.org")
        for language in languages if language not in unmatched
    )
    with patch("tools.pageviews.topics.fetch_action", side_effect=responses):
        return resolve_topic("Q1666254", list(languages), user_agent="study-tests/1 (offline)")


def pageviews_http_response(language, values):
    body = encode_items(*(
        make_item(
            f"2026070{day}00", views, project=f"{language}.wikipedia",
            article=TITLES[language].replace(" ", "_"),
        )
        for day, views in enumerate(values, start=1) if views is not None
    ))
    response = io.BytesIO(body)
    response.status = 200
    return response