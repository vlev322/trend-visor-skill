import unittest

from tests.topic_helpers import page_payload
from tools.pageviews.article_checks import check_article
from tools.pageviews.errors import PageviewsError
from tools.pageviews.topic_data import WikipediaSite


class ArticleCheckTests(unittest.TestCase):
    def setUp(self):
        self.site = WikipediaSite("cs", "cswiki", "cs.wikipedia.org")
        self.title = "Přerušovaný půst"

    def test_direct_article_with_matching_item_returns_verified_download_parameters(self):
        result = check_article(page_payload(), self.site, self.title, "Q1666254")

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["page"]["page_id"], 1632302)
        self.assertEqual(result["page"]["revision_id"], 456)
        self.assertEqual(result["page"]["wikidata_id"], "Q1666254")
        self.assertEqual(result["download_target"], {
            "project": "cs.wikipedia.org", "article": "Přerušovaný půst",
        })
        self.assertIn("cs.wikipedia.org/wiki/", result["page"]["url"])

    def test_normalization_is_not_treated_as_a_redirect(self):
        payload = page_payload()
        payload["query"]["normalized"] = [{
            "from": "přerušovaný_půst", "to": self.title,
        }]
        result = check_article(payload, self.site, "přerušovaný_půst", "Q1666254")
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["download_target"]["article"], self.title)

    def test_redirect_to_section_is_not_an_equivalent_article(self):
        payload = page_payload("Fasting", "Q44602")
        payload["query"]["redirects"] = [{
            "from": self.title, "to": "Fasting", "tofragment": "Intermittent fasting",
        }]
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("section_redirect", result["issues"])
        self.assertIn("entity_mismatch", result["issues"])
        self.assertEqual(result["page"]["title"], "Fasting")
        self.assertIsNone(result["download_target"])

    def test_even_same_item_redirect_needs_review_for_title_based_counts(self):
        payload = page_payload("New title")
        payload["query"]["redirects"] = [{"from": self.title, "to": "New title"}]
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertIn("redirect_requires_review", result["issues"])
        self.assertIsNone(result["download_target"])

    def test_redirect_chain_is_followed_without_assuming_response_order(self):
        payload = page_payload("Final")
        payload["query"]["redirects"] = [
            {"from": "Middle", "to": "Final"},
            {"from": self.title, "to": "Middle"},
        ]
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual([row["to"] for row in result["redirects"]], ["Middle", "Final"])

    def test_circular_redirect_is_explicit(self):
        payload = {"query": {"redirects": [
            {"from": self.title, "to": "Other"}, {"from": "Other", "to": self.title},
        ]}}
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual(result["issues"], ["redirect_unresolved"])
        self.assertIsNone(result["download_target"])

    def test_disambiguation_and_non_article_namespaces_need_review(self):
        for change, expected in (
            ({"pageprops": {"wikibase_item": "Q1666254", "disambiguation": ""}}, "disambiguation"),
            ({"ns": 14}, "not_article_namespace"),
            ({"pageprops": {}}, "wikidata_link_missing"),
            ({"contentmodel": "json"}, "unsupported_content_model"),
            ({"pageprops": {"wikibase_item": "Q1"}}, "entity_mismatch"),
        ):
            with self.subTest(expected=expected):
                payload = page_payload()
                payload["query"]["pages"][0].update(change)
                result = check_article(payload, self.site, self.title, "Q1666254")
                self.assertEqual(result["status"], "needs_review")
                self.assertIn(expected, result["issues"])
                self.assertIsNone(result["download_target"])

    def test_missing_page_is_not_zero_interest(self):
        payload = {"query": {"pages": [{"title": self.title, "ns": 0, "missing": True}]}}
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual(result["status"], "page_missing")
        self.assertIsNone(result["download_target"])
        self.assertNotIn("views", result)

    def test_rejects_unexplained_title_change_and_invalid_schema(self):
        payloads = (
            {}, {"query": {"pages": []}}, page_payload("Unrelated topic"),
            {**page_payload(), "continue": {"x": "y"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(PageviewsError) as caught:
                    check_article(payload, self.site, self.title, "Q1666254")
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_excerpt_is_bounded_and_output_urls_are_not_taken_from_response(self):
        payload = page_payload()
        payload["query"]["pages"][0].update({
            "extract": "x" * 1000, "canonicalurl": "https://evil.example/",
        })
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual(len(result["page"]["excerpt"]), 600)
        self.assertTrue(result["page"]["url"].startswith("https://cs.wikipedia.org/"))

    def test_redirect_badge_requires_review_even_if_page_is_currently_direct(self):
        result = check_article(
            page_payload(), self.site, self.title, "Q1666254", badges=["Q70894304"]
        )
        self.assertEqual(result["issues"], ["sitelink_redirect_badge"])
        self.assertIsNone(result["download_target"])

    def test_required_page_fields_cannot_be_missing(self):
        for field in ("contentmodel", "pageid", "lastrevid", "ns", "title"):
            with self.subTest(field=field):
                payload = page_payload()
                del payload["query"]["pages"][0][field]
                with self.assertRaises(PageviewsError) as caught:
                    check_article(payload, self.site, self.title, "Q1666254")
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_empty_or_malformed_wikidata_property_is_not_a_match(self):
        for identifier in (None, "", [], "P1"):
            with self.subTest(identifier=identifier):
                payload = page_payload(identifier=identifier)
                with self.assertRaises(PageviewsError) as caught:
                    check_article(payload, self.site, self.title, "Q1666254")
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_malformed_fragments_and_boolean_markers_are_not_matches(self):
        payload = page_payload()
        payload["query"]["redirects"] = [{
            "from": self.title, "to": self.title, "tofragment": 123,
        }]
        with self.assertRaises(PageviewsError):
            check_article(payload, self.site, self.title, "Q1666254")
        for marker in (123, None, "true"):
            with self.subTest(marker=marker):
                payload = page_payload()
                payload["query"]["pages"][0]["missing"] = marker
                with self.assertRaises(PageviewsError):
                    check_article(payload, self.site, self.title, "Q1666254")

    def test_interwiki_redirect_never_becomes_a_download_target(self):
        payload = {"query": {"interwiki": [{"title": self.title, "iw": "en"}]}}
        result = check_article(payload, self.site, self.title, "Q1666254")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["issues"], ["interwiki_redirect"])
        self.assertIsNone(result["download_target"])