import unittest

from tests.topic_helpers import entity_payload, matrix_payload
from tools.pageviews.errors import PageviewsError
from tools.pageviews.topic_data import entity_id, language_code, parse_entity, parse_sites


class SiteMatrixTests(unittest.TestCase):
    def test_uses_database_and_host_from_matrix_not_language_string_guessing(self):
        sites = parse_sites(matrix_payload(), ["be-tarask", "cs", "zz"])

        self.assertEqual(sites["be-tarask"].site_id, "be_x_oldwiki")
        self.assertEqual(sites["be-tarask"].project, "be-tarask.wikipedia.org")
        self.assertEqual(sites["cs"].site_id, "cswiki")
        self.assertIsNone(sites["zz"])

    def test_site_with_no_wikipedia_or_closed_wiki_is_not_a_valid_target(self):
        payload = matrix_payload()
        payload["sitematrix"]["0"]["site"] = []
        payload["sitematrix"]["1"]["site"][0]["closed"] = True
        sites = parse_sites(payload, ["cs", "pl"])
        self.assertIsNone(sites["cs"])
        self.assertEqual(sites["pl"].state, "closed")

    def test_ignores_other_project_families(self):
        payload = matrix_payload()
        payload["sitematrix"]["0"]["site"].insert(0, {
            "code": "wiktionary", "dbname": "cswiktionary",
            "url": "https://cs.wiktionary.org",
        })
        self.assertEqual(parse_sites(payload, ["cs"])["cs"].site_id, "cswiki")

    def test_rejects_unsafe_hosts_including_credentials_and_ports(self):
        urls = (
            "http://cs.wikipedia.org", "https://example.com",
            "https://cs.wikipedia.org.evil.example", "https://user@cs.wikipedia.org",
            "https://cs.wikipedia.org:443", "https://cs.wikipedia.org/path",
            "https://cs.wikipedia.org?host=example.com",
            "https://[",
        )
        for url in urls:
            with self.subTest(url=url):
                payload = matrix_payload()
                payload["sitematrix"]["0"]["site"][0]["url"] = url
                with self.assertRaises(PageviewsError) as caught:
                    parse_sites(payload, ["cs"])
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_incomplete_or_malformed_matrix_is_not_unsupported_language(self):
        cases = (
            {}, {"sitematrix": []}, {"sitematrix": {}}, {"sitematrix": {"count": 50}},
            {**matrix_payload(), "continue": {"smcontinue": "x"}},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(PageviewsError) as caught:
                    parse_sites(payload, ["cs"])
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_duplicate_wikipedia_sites_are_rejected(self):
        payload = matrix_payload()
        sites = payload["sitematrix"]["0"]["site"]
        sites.append(sites[0].copy())
        with self.assertRaises(PageviewsError) as caught:
            parse_sites(payload, ["cs"])
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_duplicate_language_groups_are_rejected(self):
        payload = matrix_payload()
        payload["sitematrix"]["4"] = payload["sitematrix"]["0"].copy()
        with self.assertRaises(PageviewsError) as caught:
            parse_sites(payload, ["cs"])
        self.assertEqual(caught.exception.code, "invalid_response")


class EntityDataTests(unittest.TestCase):
    def test_entity_metadata_and_sitelinks_keep_actual_language(self):
        item = parse_entity(entity_payload(), "Q1666254", "uk")
        self.assertEqual(item.identifier, "Q1666254")
        self.assertEqual(item.label, "intermittent fasting")
        self.assertEqual(item.label_language, "en")
        self.assertEqual(item.revision_id, 123)
        self.assertEqual(item.sitelinks["cswiki"]["title"], "Přerušovaný půst")
        self.assertNotIn("plwiki", item.sitelinks)

    def test_accepts_empty_wikibase_maps_without_inventing_labels(self):
        payload = entity_payload({})
        record = payload["entities"]["Q1666254"]
        for field in ("labels", "descriptions", "sitelinks"):
            record[field] = []
        item = parse_entity(payload, "Q1666254", "uk")
        self.assertEqual(item.sitelinks, {})
        self.assertIsNone(item.label)
        self.assertIsNone(item.description)

    def test_missing_item_is_not_an_empty_successful_entity(self):
        for marker in (True, ""):
            payload = {"entities": {"Q1666254": {"missing": marker}}}
            with self.subTest(marker=marker):
                with self.assertRaises(PageviewsError) as caught:
                    parse_entity(payload, "Q1666254", "en")
                self.assertEqual(caught.exception.code, "entity_unavailable")

    def test_malformed_or_different_entity_fails(self):
        for field, value in (
            ("id", "Q1"), ("type", "property"), ("lastrevid", True),
            ("labels", None), ("sitelinks", None),
        ):
            with self.subTest(field=field):
                payload = entity_payload()
                payload["entities"]["Q1666254"][field] = value
                with self.assertRaises(PageviewsError) as caught:
                    parse_entity(payload, "Q1666254", "en")
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_input_identifiers_are_validated_without_guessing(self):
        self.assertEqual(entity_id(" q1666254 "), "Q1666254")
        self.assertEqual(language_code(" CS "), "cs")
        for identifier in ("P1", "Q0", "Q1|Q2", "Q01", "https://www.wikidata.org/wiki/Q1"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(PageviewsError):
                    entity_id(identifier)