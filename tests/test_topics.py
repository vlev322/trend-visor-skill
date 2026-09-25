import unittest
from unittest.mock import patch

from tests.topic_helpers import action_response, entity_payload, matrix_payload, page_payload
from tools.pageviews.errors import PageviewsError
from tools.pageviews.topics import resolve_topic, search_topics


class TopicSearchTests(unittest.TestCase):
    @patch("tools.pageviews.topics.fetch_action")
    def test_search_returns_candidates_without_choosing_an_item(self, fetch):
        fetch.return_value = action_response({
            "searchinfo": {"search": "піст"},
            "search": [
                {
                    "id": "Q1666254", "label": "Інтервальне голодування",
                    "description": "Synthetic test description",
                    "display": {"label": {
                        "value": "Інтервальне голодування", "language": "uk"
                    }},
                    "match": {"type": "alias", "text": "піст", "language": "uk"},
                },
                {"id": "Q44602", "label": "fasting"},
            ],
            "search-continue": 2,
            "success": 1,
        })

        result = search_topics("піст", language="uk", limit=2, user_agent="test/1")

        self.assertEqual(result["status"], "candidates_found")
        self.assertTrue(result["requires_selection"])
        self.assertNotIn("selected_entity", result)
        self.assertEqual([row["entity_id"] for row in result["candidates"]], [
            "Q1666254", "Q44602",
        ])
        self.assertEqual(result["candidates"][0]["label_language"], "uk")
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["source"], fetch.return_value.source.source_metadata())
        parameters = fetch.call_args.args[1]
        self.assertEqual(parameters["language"], "uk")
        self.assertEqual(parameters["type"], "item")
        fetch.assert_called_once()

    @patch("tools.pageviews.topics.fetch_action")
    def test_empty_search_is_explicit_and_not_an_error(self, fetch):
        fetch.return_value = action_response({"search": [], "success": 1})
        result = search_topics("Unknown topic", user_agent="test/1")
        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["requires_selection"])
        self.assertEqual(result["next_action"], "refine_query_or_language")

    @patch("tools.pageviews.topics.fetch_action")
    def test_one_candidate_is_not_automatically_selected(self, fetch):
        fetch.return_value = action_response({"search": [{"id": "Q1"}], "success": 1})
        result = search_topics("Universe", user_agent="test/1")
        self.assertTrue(result["requires_selection"])
        self.assertIsNone(result["candidates"][0]["label"])
        self.assertNotIn("selected_entity", result)

    @patch("tools.pageviews.topics.fetch_action")
    def test_fallback_language_and_search_offset_are_visible(self, fetch):
        fetch.return_value = action_response({"search": [{
            "id": "Q1", "display": {
                "label": {"language": "en", "value": "Universe"},
                "description": {"language": "en", "value": "All space and time"},
            },
        }], "success": 1})
        result = search_topics("всесвіт", language=" UK ", offset=5, user_agent="test/1")
        self.assertEqual(result["language"], "uk")
        self.assertEqual(result["candidates"][0]["label_language"], "en")
        self.assertEqual(fetch.call_args.args[1]["continue"], 5)

    @patch("tools.pageviews.topics.fetch_action")
    def test_invalid_search_inputs_do_not_reach_network(self, fetch):
        cases = (
            {"query": " "}, {"query": "a\nb"}, {"query": "x" * 501},
            {"language": "cs.wikipedia.org"}, {"limit": 0}, {"limit": True},
            {"limit": 51}, {"offset": -1}, {"timeout": float("nan")},
            {"user_agent": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                parameters = {"query": "Topic", "user_agent": "test/1", **changes}
                with self.assertRaises(PageviewsError) as caught:
                    search_topics(**parameters)
                self.assertEqual(caught.exception.code, "invalid_request")
        fetch.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_malformed_responses_do_not_become_no_candidates(self, fetch):
        cases = (
            {}, {"search": {}}, {"search": [{"id": "P1"}]},
            {"search": [{"id": "Q1"}, {"id": "Q1"}]},
            {"search": [], "search-continue": True},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                fetch.return_value = action_response(payload)
                with self.assertRaises(PageviewsError) as caught:
                    search_topics("Topic", user_agent="test/1")
                self.assertEqual(caught.exception.code, "invalid_response")


class TopicResolutionTests(unittest.TestCase):
    @patch("tools.pageviews.topics.fetch_action")
    def test_keeps_missing_sitelink_and_checks_available_article(self, fetch):
        fetch.side_effect = [
            action_response(matrix_payload()),
            action_response(entity_payload()),
            action_response(page_payload(), "cs.wikipedia.org"),
        ]

        result = resolve_topic("Q1666254", ["pl", "cs"], user_agent="test/1")

        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual([target["language"] for target in result["targets"]], ["pl", "cs"])
        polish, czech = result["targets"]
        self.assertEqual(polish["status"], "no_sitelink")
        self.assertIsNone(polish["download_target"])
        self.assertEqual(czech["status"], "matched")
        self.assertEqual(czech["download_target"]["article"], "Přerušovaný půst")
        self.assertEqual(result["summary"]["matched"], 1)
        self.assertEqual(fetch.call_count, 3)
        calls = fetch.call_args_list
        self.assertEqual(calls[0].args[1]["action"], "sitematrix")
        self.assertEqual(calls[1].args[1]["sitefilter"], "plwiki|cswiki")
        self.assertEqual(calls[1].args[1]["redirects"], "no")
        self.assertEqual(calls[2].args[0], "cs.wikipedia.org")
        self.assertEqual(calls[2].args[1]["titles"], "Přerušovaný půst")

    @patch("tools.pageviews.topics.fetch_action")
    def test_all_matches_still_require_confirmation(self, fetch):
        fetch.side_effect = [
            action_response(matrix_payload()), action_response(entity_payload()),
            action_response(page_payload()),
        ]
        result = resolve_topic("q1666254", ["CS"], user_agent="test/1")
        self.assertEqual(result["status"], "ready_for_confirmation")
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["targets"][0]["language"], "cs")
        self.assertEqual(result["entity"]["revision_id"], 123)

    @patch("tools.pageviews.topics.fetch_action")
    def test_unsupported_closed_and_missing_targets_do_not_trigger_page_queries(self, fetch):
        matrix = matrix_payload()
        matrix["sitematrix"]["0"]["site"][0]["closed"] = True
        fetch.side_effect = [action_response(matrix), action_response(entity_payload())]
        result = resolve_topic("Q1666254", ["zz", "cs", "pl"], user_agent="test/1")
        self.assertEqual([target["status"] for target in result["targets"]], [
            "unsupported_language", "unavailable_project", "no_sitelink",
        ])
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(fetch.call_count, 2)

    @patch("tools.pageviews.topics.fetch_action")
    def test_partial_network_failure_remains_visible_with_other_match(self, fetch):
        links = {
            "cswiki": {"site": "cswiki", "title": "Czech title", "badges": []},
            "plwiki": {"site": "plwiki", "title": "Polish title", "badges": []},
        }
        fetch.side_effect = [
            action_response(matrix_payload()), action_response(entity_payload(links)),
            PageviewsError("network_error", "offline"),
            action_response(page_payload("Polish title"), "pl.wikipedia.org"),
        ]
        result = resolve_topic("Q1666254", ["cs", "pl"], user_agent="test/1")
        self.assertEqual(result["targets"][0]["status"], "check_failed")
        self.assertEqual(result["targets"][0]["error"]["code"], "network_error")
        self.assertEqual(result["targets"][1]["status"], "matched")
        self.assertEqual(result["summary"]["failed_checks"], 1)

    @patch("tools.pageviews.topics.fetch_action")
    def test_rate_limit_stops_further_requests_but_keeps_all_languages(self, fetch):
        links = {
            "cswiki": {"site": "cswiki", "title": "Czech title", "badges": []},
            "plwiki": {"site": "plwiki", "title": "Polish title", "badges": []},
        }
        fetch.side_effect = [
            action_response(matrix_payload()), action_response(entity_payload(links)),
            PageviewsError("rate_limited", "wait", details={"retry_after": "60"}),
        ]
        result = resolve_topic("Q1666254", ["cs", "pl", "en"], user_agent="test/1")
        self.assertEqual([target["status"] for target in result["targets"]], [
            "check_failed", "not_checked", "no_sitelink",
        ])
        self.assertEqual(result["targets"][0]["error"]["details"]["retry_after"], "60")
        self.assertEqual(fetch.call_count, 3)

    @patch("tools.pageviews.topics.fetch_action")
    def test_malformed_sitelink_is_not_silently_treated_as_absent(self, fetch):
        fetch.side_effect = [
            action_response(matrix_payload()),
            action_response(entity_payload({"cswiki": {"site": "plwiki", "title": "Wrong"}})),
        ]
        result = resolve_topic("Q1666254", ["cs"], user_agent="test/1")
        self.assertEqual(result["targets"][0]["status"], "check_failed")
        self.assertEqual(result["targets"][0]["error"]["code"], "invalid_response")
        self.assertEqual(fetch.call_count, 2)

    @patch("tools.pageviews.topics.fetch_action")
    def test_identity_mismatch_is_not_a_download_target(self, fetch):
        fetch.side_effect = [
            action_response(matrix_payload()), action_response(entity_payload()),
            action_response(page_payload(identifier="Q44602")),
        ]
        result = resolve_topic("Q1666254", ["cs"], user_agent="test/1")
        target = result["targets"][0]
        self.assertEqual(target["status"], "needs_review")
        self.assertIn("entity_mismatch", target["issues"])
        self.assertIsNone(target["download_target"])

    @patch("tools.pageviews.topics.fetch_action")
    def test_invalid_inputs_are_rejected_before_network(self, fetch):
        cases = (
            {"identifier": "Topic"}, {"languages": []}, {"languages": "cs"},
            {"languages": ["cs", "CS"]}, {"languages": ["https://cs.wikipedia.org"]},
            {"languages": ["cs"] * 51}, {"label_language": "en|uk"},
            {"timeout": 0}, {"user_agent": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                inputs = {"identifier": "Q1666254", "languages": ["cs"],
                          "user_agent": "test/1", **changes}
                with self.assertRaises(PageviewsError) as caught:
                    resolve_topic(**inputs)
                self.assertEqual(caught.exception.code, "invalid_request")
        fetch.assert_not_called()