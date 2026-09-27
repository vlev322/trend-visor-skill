import io
import json
import hashlib
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helpers import encode_items, make_item
from tests.topic_helpers import action_response, entity_payload, matrix_payload, page_payload
from tools.pageviews.artifacts import JsonArtifact, save_json_artifact
from tools.pageviews.discovery import (
    approve_discovery, begin_discovery, collect_discovery, discovery_summary,
    read_discovery, resolve_discovery, revise_discovery, search_discovery,
)
from tools.pageviews.errors import PageviewsError
from tools.pageviews.research_state import create_research, research_report

QUESTION = "Порівняй перегляди статей про міське садівництво."
SCOPE = {
    "query": "urban gardening", "search_language": "en", "languages": ["cs", "pl"],
    "baseline_start": "2026-07-01", "baseline_end": "2026-07-02",
    "current_start": "2026-07-03", "current_end": "2026-07-04", "as_of": "2026-09-25",
    "lag_days": 7, "methodology": None,
}


def lookup_responses():
    identifier = "Q9001"
    titles = {"cs": "Městské zahradničení", "pl": "Ogrodnictwo miejskie"}
    entity = entity_payload({f"{lang}wiki": {"site": f"{lang}wiki", "title": title, "badges": []}
                             for lang, title in titles.items()})
    item = entity["entities"].pop("Q1666254")
    item.update(id=identifier, labels={"en": {"language": "en", "value": "Urban gardening"}})
    entity["entities"][identifier] = item
    return [action_response({"search": [{"id": identifier, "label": "Urban gardening"}]}),
            action_response(matrix_payload()), action_response(entity),
            *(action_response(page_payload(title, identifier), f"{lang}.wikipedia.org") for lang, title in titles.items())]


def views_response(language, title, values):
    response = io.BytesIO(encode_items(*(make_item(f"2026070{day}00", value, project=f"{language}.wikipedia", article=title)
                                        for day, value in enumerate(values, 1))))
    response.status = 200
    return response


class DiscoveryTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No network"))
    def test_question_can_be_saved_before_scope_and_clarified_without_losing_history(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, output=root / "initial.json")
            self.assertEqual(read_discovery(initial)["status"], "needs_scope")
            ready = revise_discovery(initial, question=QUESTION, scope=SCOPE, output=root / "ready.json")
            state = read_discovery(ready)
            self.assertEqual(state["scope"], SCOPE)
            self.assertEqual(state["status"], "ready_to_search")
            self.assertEqual(state["parent"]["sha256"], initial.sha256)
            self.assertEqual(state["discovery_id"], read_discovery(initial)["discovery_id"])
            self.assertIsNone(read_discovery(initial)["scope"])
        network.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    @patch("tools.pageviews.topics.fetch_action")
    def test_search_review_approval_collection_and_report_are_connected(self, lookup, network):
        lookup.side_effect = lookup_responses()
        network.side_effect = [views_response("cs", "Městské_zahradničení", (10, 20, 30, 60)),
                               views_response("pl", "Ogrodnictwo_miejskie", (100, 200, 200, 400))]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            searched = search_discovery(initial, user_agent="discovery-tests/1", output=root / "searched.json")
            self.assertEqual(discovery_summary(searched)["search_result"]["candidates"][0]["entity_id"], "Q9001")
            with self.assertRaises(PageviewsError):
                resolve_discovery(searched, entity="Q333", user_agent="discovery-tests/1", output=root / "wrong.json")
            pending = resolve_discovery(searched, entity="Q9001", user_agent="discovery-tests/1", output=root / "pending.json")
            self.assertEqual(read_discovery(pending)["status"], "awaiting_confirmation")
            with self.assertRaises(PageviewsError) as caught:
                collect_discovery(pending, cache_dir=root / "cache", output=root / "blocked.json")
            self.assertEqual(caught.exception.code, "confirmation_required")
            network.assert_not_called()
            approved = approve_discovery(pending, confirmation=pending.sha256, user_reply="Погоджую обидві статті й дати.",
                                          output=root / "approved.json")
            done = collect_discovery(approved, cache_dir=root / "cache", online=True, user_agent="discovery-tests/1",
                                      output=root / "done.json")
            summary = discovery_summary(done)
            self.assertEqual(summary["status"], "collected")
            self.assertEqual(summary["study_summary"]["analyzed_languages"], 2)
            reference = JsonArtifact(Path(summary["research"]["path"]), summary["research"]["sha256"])
            report = research_report(reference)
            self.assertEqual(report["question"], QUESTION)
            self.assertEqual([row["analysis"]["comparison"]["change_percent"] for row in report["evidence"]], [200.0, 100.0])
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(discovery_summary(done), summary)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertEqual(lookup.call_count, 5)
            self.assertEqual(network.call_count, 2)

    def approved(self, root, lookup):
        lookup.side_effect = lookup_responses()
        initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
        searched = search_discovery(initial, user_agent="discovery-tests/1", output=root / "searched.json")
        pending = resolve_discovery(searched, entity="Q9001", user_agent="discovery-tests/1", output=root / "pending.json")
        approved = approve_discovery(pending, confirmation=pending.sha256, user_reply="Тестова згода.", output=root / "approved.json")
        return initial, searched, pending, approved

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No HTTP"))
    def test_invalid_scope_and_unapproved_collection_do_not_fetch_or_write(self, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for key, value in (("languages", []), ("languages", ["cs", "cs"]), ("languages", "cs"),
                               ("current_start", "2026-07-02"), ("baseline_start", "2015-06-01"),
                               ("current_end", "2026-09-25"), ("lag_days", True), ("methodology", {"hidden": True})):
                scope = {**SCOPE, key: value}
                with self.subTest(key=key, value=value), self.assertRaises(PageviewsError):
                    begin_discovery(question=QUESTION, scope=scope, output=root / "invalid.json")
                self.assertFalse((root / "invalid.json").exists())
            initial = begin_discovery(question=QUESTION, output=root / "question.json")
            with self.assertRaises(PageviewsError):
                collect_discovery(initial, online=True, user_agent="discovery-tests/1", cache_dir=root / "cache", output=root / "blocked.json")
            self.assertFalse((root / "blocked.json").exists())
        network.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_empty_search_and_explicit_pagination_preserve_pages(self, lookup):
        lookup.side_effect = [action_response({"search": [], "search-continue": 5}),
                              action_response({"search": [{"id": "Q9001", "label": "Urban gardening"}]})]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            empty = search_discovery(initial, user_agent="discovery-tests/1", output=root / "empty.json")
            self.assertEqual(discovery_summary(empty)["search_result"]["status"], "no_candidates")
            with self.assertRaises(PageviewsError):
                resolve_discovery(empty, entity="Q9001", user_agent="discovery-tests/1", output=root / "wrong.json")
            next_page = search_discovery(empty, next_page=True, user_agent="discovery-tests/1", output=root / "page2.json")
            self.assertEqual(discovery_summary(next_page)["search_result"]["offset"], 5)
            self.assertEqual(discovery_summary(empty)["search_result"]["candidates"], [])
            with self.assertRaises(PageviewsError):
                search_discovery(next_page, next_page=True, user_agent="discovery-tests/1", output=root / "page3.json")
            self.assertEqual(lookup.call_count, 2)

    @patch("tools.pageviews.topics.fetch_action")
    def test_network_failure_is_saved_and_not_retried_when_reading(self, lookup):
        lookup.side_effect = PageviewsError("api_busy", "Fixture lag")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            failed = search_discovery(initial, user_agent="discovery-tests/1", output=root / "failed.json")
            summary = discovery_summary(failed)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["error"]["error"]["code"], "api_busy")
            self.assertEqual(summary["operations"], 1)
            self.assertEqual(summary, discovery_summary(failed))
            with self.assertRaises(PageviewsError):
                search_discovery(failed, user_agent="discovery-tests/1", output=root / "retry.json")
            ready = revise_discovery(failed, question=QUESTION, scope=SCOPE, output=root / "revised.json")
            self.assertEqual(read_discovery(ready)["operations"], 1)
            lookup.assert_called_once()

    @patch("tools.pageviews.topics.fetch_action")
    def test_resolve_failure_keeps_selection_and_clears_no_other_evidence(self, lookup):
        lookup.side_effect = [lookup_responses()[0], PageviewsError("api_busy", "Fixture lag")]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            searched = search_discovery(initial, user_agent="test/1", output=root / "searched.json")
            failed = resolve_discovery(searched, entity="Q9001", user_agent="test/1", output=root / "failed.json")
            state = read_discovery(failed)
            self.assertEqual(state["selection"], "Q9001")
            self.assertEqual(state["error"]["operation"], "resolve")
            self.assertIsNone(state["resolution"])
            self.assertEqual(lookup.call_count, 2)

    @patch("tools.pageviews.topics.fetch_action")
    def test_collection_failure_checkpoint_does_not_keep_active_approval(self, lookup):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, approved = self.approved(root, lookup)
            with patch("tools.pageviews.discovery.run_study", side_effect=PageviewsError("missing_dependency", "Fixture missing statistics")) as run:
                failed = collect_discovery(approved, cache_dir=root / "cache", output=root / "failed.json")
                run.assert_called_once()
            state = read_discovery(failed)
            self.assertEqual(state["status"], "failed")
            self.assertIsNone(state["approval"])
            self.assertEqual(state["error"]["operation"], "collect")
            self.assertFalse((root / "failed-study.json").exists())
            self.assertEqual(read_discovery(approved)["status"], "approved")

    @patch("tools.pageviews.client.urlopen")
    @patch("tools.pageviews.topics.fetch_action")
    def test_explicit_calendar_methodology_and_text_criteria_survive_handoff(self, lookup, network):
        lookup.side_effect = lookup_responses()
        network.side_effect = [views_response("cs", "Městské_zahradničení", (10, 20, 30, 60)),
                               views_response("pl", "Ogrodnictwo_miejskie", (100, 200, 200, 400))]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            method = {"trend_model": None, "hac_lags": None}
            initial = begin_discovery(question=QUESTION, criteria=["Перевірити сезонність"],
                                      scope={**SCOPE, "methodology": method}, output=root / "initial.json")
            searched = search_discovery(initial, user_agent="test/1", output=root / "searched.json")
            pending = resolve_discovery(searched, entity="Q9001", user_agent="test/1", output=root / "pending.json")
            approved = approve_discovery(pending, confirmation=pending.sha256, user_reply="Тест", output=root / "approved.json")
            done = collect_discovery(approved, cache_dir=root / "cache", output=root / "done.json", online=True, user_agent="test/1")
            state = read_discovery(done)
            study = json.loads(Path(state["study"]["path"]).read_bytes())
            self.assertEqual(study["methodology"]["parameters"], method)
            self.assertEqual(discovery_summary(done)["limits"]["online_collection_http_upper_bound"], 2)
            report = research_report(JsonArtifact(Path(state["research"]["path"]), state["research"]["sha256"]))
            self.assertEqual(report["criteria"], ["Перевірити сезонність"])
            self.assertEqual(report["prioritization"]["status"], "criteria_require_review")

    @patch("tools.pageviews.topics.fetch_action")
    def test_changed_context_clears_selection_resolution_and_approval(self, lookup):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, approved = self.approved(root, lookup)
            changed = revise_discovery(approved, question="Інша тема", scope={**SCOPE, "query": "public transport"}, output=root / "changed.json")
            state = read_discovery(changed)
            self.assertEqual(state["status"], "ready_to_search")
            for key in ("selection", "resolution", "search", "approval", "study", "research"):
                self.assertIsNone(state[key])
            self.assertEqual(read_discovery(approved)["status"], "approved")

    @patch("tools.pageviews.topics.fetch_action")
    def test_confirmation_binds_scope_and_source_even_after_state_is_rehashed(self, lookup):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, pending, approved = self.approved(root, lookup)
            with self.assertRaises(PageviewsError):
                approve_discovery(pending, confirmation="0" * 64, user_reply="Так", output=root / "bad.json")
            original = read_discovery(approved)
            for index, (keys, value) in enumerate(((('scope', 'current_end'), '2026-07-05'),
                                                   (('question',), 'Changed'), (('selection',), 'Q333'),
                                                   (('approval', 'user_reply'), ''), (('revision',), 99))):
                changed = deepcopy(original)
                target = changed
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                artifact = save_json_artifact(changed, root / f"tampered-{index}.json")
                with self.subTest(keys=keys), self.assertRaises(PageviewsError):
                    read_discovery(artifact)
            pending.path.write_bytes(pending.path.read_bytes() + b"\n")
            with self.assertRaises(PageviewsError):
                read_discovery(approved)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Offline means no HTTP"))
    @patch("tools.pageviews.topics.fetch_action")
    def test_offline_cache_miss_is_a_preserved_partial_handoff(self, lookup, network):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, approved = self.approved(root, lookup)
            result = collect_discovery(approved, cache_dir=root / "cache", output=root / "offline.json")
            summary = discovery_summary(result)
            self.assertEqual(summary["study_summary"]["failed_collections"], 2)
            research = JsonArtifact(Path(summary["research"]["path"]), summary["research"]["sha256"])
            self.assertEqual([row["reason"] for row in research_report(research)["evidence"]], ["cache_miss", "cache_miss"])
            network.assert_not_called()

    @patch("tools.pageviews.topics.fetch_action")
    def test_unmatched_languages_are_kept_and_no_match_cannot_be_approved(self, lookup):
        for keep_cs in (True, False):
            with self.subTest(keep_cs=keep_cs), TemporaryDirectory() as directory:
                responses = lookup_responses()
                payload = responses[2].payload
                links = payload["entities"]["Q9001"]["sitelinks"]
                links.pop("plwiki")
                if not keep_cs:
                    links.pop("cswiki")
                responses[2] = action_response(payload)
                lookup.side_effect = responses[:4] if keep_cs else responses[:3]
                root = Path(directory)
                initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
                searched = search_discovery(initial, user_agent="test/1", output=root / "searched.json")
                pending = resolve_discovery(searched, entity="Q9001", user_agent="test/1", output=root / "pending.json")
                result = discovery_summary(pending)["resolution"]["result"]
                self.assertEqual(result["targets"][1]["status"], "no_sitelink")
                if keep_cs:
                    approved = approve_discovery(pending, confirmation=pending.sha256, user_reply="Тест", output=root / "approved.json")
                    self.assertEqual(read_discovery(approved)["status"], "approved")
                else:
                    self.assertEqual(discovery_summary(pending)["next_action"], "clarify_scope_or_mapping")
                    with self.assertRaises(PageviewsError):
                        approve_discovery(pending, confirmation=pending.sha256, user_reply="Тест", output=root / "approved.json")

    @patch("tools.pageviews.topics.fetch_action")
    def test_collected_methodology_must_match_approved_scope(self, lookup):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, approved = self.approved(root, lookup)
            collected = collect_discovery(approved, cache_dir=root / "cache", output=root / "collected.json")
            state = read_discovery(collected)
            study = json.loads(Path(state["study"]["path"]).read_bytes())
            study["methodology"] = {"parameters": {"trend_model": None, "hac_lags": None}}
            replaced = save_json_artifact(study, root / "replaced-study.json")
            post = create_research(replaced, question=QUESTION, output=root / "replaced-research.json")
            state["study"] = {"path": str(replaced.path), "sha256": replaced.sha256}
            state["research"] = {"path": str(post.path), "sha256": post.sha256}
            changed = save_json_artifact(state, root / "changed.json")
            with self.assertRaises(PageviewsError) as caught:
                read_discovery(changed)
            self.assertEqual(caught.exception.code, "discovery_state_error")

    @patch("tools.pageviews.topics.fetch_action")
    def test_existing_sidecar_and_exhausted_budget_block_requests(self, lookup):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            initial = begin_discovery(question=QUESTION, scope=SCOPE, output=root / "initial.json")
            (root / "blocked-search.json").write_text("preserve")
            with self.assertRaises(PageviewsError):
                search_discovery(initial, user_agent="test/1", output=root / "blocked.json")
            with patch("tools.pageviews.discovery.MAX_OPERATIONS", 0), self.assertRaises(PageviewsError) as caught:
                search_discovery(initial, user_agent="test/1", output=root / "budget.json")
            self.assertEqual(caught.exception.code, "discovery_budget_exhausted")
            lookup.assert_not_called()
            self.assertEqual((root / "blocked-search.json").read_text(), "preserve")

    def test_malformed_state_and_scope_checksums_are_structured_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ref = begin_discovery(question=QUESTION, output=root / "initial.json")
            body = ref.path.read_bytes()
            for sha in ("é" * 64, "0" * 64, "bad"):
                with self.subTest(sha=sha), self.assertRaises(PageviewsError):
                    read_discovery(JsonArtifact(ref.path, sha))
            for invalid in (b"[]", b"{}", b"{bad", b'{"scope":null,' + body[1:], b'{"value":NaN,' + body[1:]):
                ref.path.write_bytes(invalid)
                with self.subTest(body=invalid[:20]), self.assertRaises(PageviewsError):
                    read_discovery(JsonArtifact(ref.path, hashlib.sha256(invalid).hexdigest()))