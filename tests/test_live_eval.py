import io
import hashlib
import json
import os
import subprocess
import sys
import unittest
from datetime import date, timedelta
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.final_answer_helpers import model_fields
from tests.helpers import make_item
from tests.study_helpers import resolution_result
from tests.topic_helpers import entity_payload, matrix_payload, page_payload
from tools.model_eval.live import read_resume, run_session
from tools.model_eval.live_tools import LiveSession
from tools.pageviews.errors import PageviewsError
from tools.pageviews.resolutions import read_resolution, save_resolution


def http(payload):
    response = io.BytesIO(json.dumps(payload, ensure_ascii=False).encode())
    response.status = 200
    return response


def model_call(arguments):
    return {
        "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call-test", "type": "function", "function": {
                "name": "wikipedia_research", "arguments": json.dumps(arguments),
            }}],
        },
    }


def resume_checkpoint():
    return {
        "phase": "discover", "status": "needs_user_input", "scope": {},
        "skill_sha256": "0" * 64, "adapter_instructions": "Fixture",
        "model_calls": 1, "events": [{
            "response": {"finish_reason": "stop", "message": {"content": "Уточніть тему.", "tool_calls": []}},
        }],
        "operation_results": {},
    }


class LiveDiscoveryTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen")
    def test_discovery_reuses_real_cli_and_stops_before_collection(self, network):
        network.side_effect = [
            http({"search": [{"id": "Q1666254", "label": "intermittent fasting"}]}),
            http(matrix_payload()), http(entity_payload()), http(page_payload()),
        ]
        with TemporaryDirectory() as directory:
            session = LiveSession("discover", Path(directory))
            search = session.execute({"operation": "search", "query": "інтервальне голодування"})
            self.assertEqual(search["candidates"][0]["entity_id"], "Q1666254")
            resolution = session.execute({"operation": "resolve", "entity": "Q1666254"})
            self.assertEqual([row["status"] for row in resolution["targets"]], ["no_sitelink", "matched"])
            result = session.finish("Підтвердьте чеську статтю; польська відповідність відсутня.")
            self.assertEqual(result["status"], "awaiting_confirmation")
            saved = resolution["artifacts"]
            plan = read_resolution(Path(saved["resolution"]), saved["resolution_sha256"])
            self.assertEqual(plan.entity["entity_id"], "Q1666254")
            with self.assertRaises(PageviewsError):
                session.execute({"operation": "study", "mode": "fresh"})
            self.assertEqual(network.call_count, 4)

    @patch("tools.pageviews.client.urlopen")
    def test_unapproved_actions_and_paths_never_reach_network(self, network):
        with TemporaryDirectory() as directory:
            session = LiveSession("discover", Path(directory))
            for arguments in (
                {"operation": "resolve", "entity": "Q1666254"},
                {"operation": "run_shell", "query": "anything"},
                {"operation": "search", "query": "topic", "output": ".env"},
                {"operation": "chart", "language": "cs"},
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(PageviewsError):
                        session.execute(arguments)
        network.assert_not_called()

    @patch("tools.pageviews.client.urlopen")
    def test_api_lag_stops_model_and_wikimedia_without_retry(self, network):
        network.return_value = http({"error": {"code": "maxlag"}})
        model_requests = []

        def complete(messages, schema):
            model_requests.append(messages)
            return model_call({"operation": "search", "query": "інтервальне голодування"})

        with TemporaryDirectory() as directory:
            session = LiveSession("discover", Path(directory))
            report = run_session(session, complete, "Skill instructions")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["details"]["result"]["error"]["code"], "api_busy")
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(len(model_requests), 1)
        network.assert_called_once()

    @patch("tools.pageviews.client.urlopen")
    def test_selection_and_duplicate_operation_guards(self, network):
        network.return_value = http({"search": [{"id": "Q1666254", "label": "intermittent fasting"}]})
        with TemporaryDirectory() as directory:
            session = LiveSession("discover", Path(directory))
            session.execute({"operation": "search", "query": "topic"})
            for action in (
                {"operation": "resolve", "entity": "Q1"},
                {"operation": "search", "query": "another topic"},
            ):
                with self.assertRaises(PageviewsError):
                    session.execute(action)
        network.assert_called_once()

    def test_research_requires_exact_confirmed_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = save_resolution(resolution_result(("pl", "cs"), unmatched=("pl",)), root / "resolution.json")
            for options in ({}, {"resolution": saved.path}, {"resolution": saved.path, "confirmation": "0" * 64}):
                with self.subTest(options=options):
                    with self.assertRaises(PageviewsError):
                        LiveSession("research", root / "output", **options)

    def test_help_works_without_extras_or_credentials(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "tools.model_eval.live", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--confirm-sha256", result.stdout)

    @patch("tools.pageviews.client.urlopen")
    def test_invalid_model_tool_shapes_are_structured_failures(self, network):
        for call in ("not-an-object", {"type": "function", "id": "call-test", "function": None},
                     {"type": "function", "id": 123, "function": {"name": "wikipedia_research"}}):
            with self.subTest(call=call), TemporaryDirectory() as directory:
                response = {"finish_reason": "tool_calls", "message": {"tool_calls": [call]}}
                session = LiveSession("discover", Path(directory))
                result = run_session(session, lambda messages, schema: response, "Skill")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], "invalid_live_tool")
                self.assertEqual(result["model_calls"], 1)
        network.assert_not_called()

    def test_astronomy_profile_passes_the_users_question_and_only_uk(self):
        with TemporaryDirectory() as directory:
            session = LiveSession("discover", Path(directory), profile="astronomy-uk")

            def complete(messages, schema):
                scope = json.loads(messages[-1]["content"])
                self.assertEqual(scope["languages"], ["uk"])
                self.assertEqual(scope["topic"], "астрономія")
                self.assertIn("курс з астрономії", scope["question"])
                self.assertEqual(scope["periods"]["current-end"], "2026-09-18")
                self.assertEqual(scope["as_of"], "2026-09-26")
                self.assertEqual(schema["function"]["parameters"]["properties"]["language"]["enum"], ["uk"])
                return {"finish_reason": "stop", "message": {"content": "Уточніть межі теми.", "tool_calls": []}}

            result = run_session(session, complete, "Skill")
            self.assertEqual(result["status"], "needs_user_input")

    @patch("tools.pageviews.client.urlopen")
    def test_human_clarification_resumes_without_searching_again(self, network):
        network.side_effect = [
            http({"search": [{"id": "Q1666254", "label": "intermittent fasting"}]}),
            http(matrix_payload()), http(entity_payload()), http(page_payload()),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = LiveSession("discover", root / "first")
            replies = iter((
                model_call({"operation": "search", "query": "інтервальне голодування"}),
                {"finish_reason": "stop", "message": {"content": "Підтвердьте Q1666254.", "tool_calls": []}},
            ))
            paused = run_session(first, lambda messages, schema: next(replies), "Skill")
            before = json.dumps(paused, sort_keys=True)
            second = LiveSession("discover", root / "second")
            resumed_replies = iter((
                model_call({"operation": "resolve", "entity": "Q1666254"}),
                {"finish_reason": "stop", "message": {"content": "Підтвердьте чеську статтю.", "tool_calls": []}},
            ))
            requests = []

            def complete(messages, schema):
                requests.append(list(messages))
                return next(resumed_replies)

            result = run_session(second, complete, "Skill", resume=paused, user_reply="Так, обираю Q1666254.")
            self.assertEqual(result["status"], "awaiting_confirmation")
            self.assertEqual(result["model_calls"], 4)
            self.assertEqual(result["new_model_calls"], 2)
            self.assertEqual(result["previous_model_calls"], 2)
            self.assertEqual(requests[0][-1], {"role": "user", "content": "Так, обираю Q1666254."})
            self.assertTrue(any(row["role"] == "tool" for row in requests[0]))
            self.assertEqual(json.dumps(paused, sort_keys=True), before)
            self.assertEqual(network.call_count, 4)
            with self.assertRaises(PageviewsError):
                run_session(LiveSession("discover", root / "wrong", profile="astronomy-uk"), complete,
                            "Skill", resume=paused, user_reply="Так")

    def test_resume_file_is_bound_to_its_exact_checksum(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.json"
            body = json.dumps(resume_checkpoint()).encode()
            path.write_bytes(body)
            checksum = hashlib.sha256(body).hexdigest()
            self.assertEqual(read_resume(path, checksum)["status"], "needs_user_input")
            path.write_bytes(body + b"\n")
            with self.assertRaises(PageviewsError):
                read_resume(path, checksum)

    def test_malformed_resume_checksum_is_a_structured_invalid_request(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.json"
            path.write_text('{"status":"needs_user_input"}')
            for checksum in ("é" * 64, "A" * 64, "0" * 63, "g" * 64):
                with self.subTest(checksum=checksum), self.assertRaises(PageviewsError) as caught:
                    read_resume(path, checksum)
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_malformed_resume_events_are_structured_invalid_requests(self):
        malformed_events = (
            [{}],
            [{"response": {}}],
            [{"response": {"message": {"content": None, "tool_calls": {}}}}],
            [{"response": {"message": {"content": None, "tool_calls": []}}, "result": {}}],
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.json"
            for events in malformed_events:
                with self.subTest(events=events):
                    checkpoint = resume_checkpoint()
                    checkpoint["events"] = events
                    body = json.dumps(checkpoint).encode()
                    path.write_bytes(body)
                    with self.assertRaises(PageviewsError) as caught:
                        read_resume(path, hashlib.sha256(body).hexdigest())
                    self.assertEqual(caught.exception.code, "invalid_request")

    def test_resume_rejects_duplicate_fields_and_non_finite_json(self):
        valid = json.dumps(resume_checkpoint())
        malformed_documents = (
            valid.replace('{"phase":', '{"status":"needs_user_input","phase":', 1),
            valid[:-1] + ',"unexpected":NaN}',
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "live.json"
            for document in malformed_documents:
                with self.subTest(document=document):
                    body = document.encode()
                    path.write_bytes(body)
                    with self.assertRaises(PageviewsError) as caught:
                        read_resume(path, hashlib.sha256(body).hexdigest())
                    self.assertEqual(caught.exception.code, "invalid_request")


@unittest.skipUnless(
    find_spec("statsmodels") is not None and find_spec("matplotlib") is not None,
    "Optional statistics and charts extras are not installed",
)
class LiveResearchTests(unittest.TestCase):
    @patch("tools.pageviews.client.urlopen")
    def test_full_live_interface_uses_snapshots_and_checks_final_facts(self, network):
        start, end = date(2024, 9, 18), date(2026, 9, 17)
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        network.return_value = http({"items": [
            make_item(
                day.strftime("%Y%m%d00"), 10 if day < date(2025, 9, 18) else 20,
                article="Czech_test_article",
            )
            for day in days if day != date(2026, 7, 13)
        ]})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = save_resolution(resolution_result(("pl", "cs"), unmatched=("pl",)), root / "resolution.json")
            session = LiveSession(
                "research", root / "run", resolution=saved.path,
                confirmation=saved.sha256, cache_dir=root / "cache",
            )
            actions = iter((
                {"operation": "study", "mode": "fresh"},
                {"operation": "analyze", "language": "cs"},
                {"operation": "chart", "language": "cs"},
                {"operation": "study", "mode": "offline"},
            ))
            requests = []

            def complete(messages, schema, **options):
                requests.append(messages)
                action = next(actions, None)
                if action is not None:
                    self.assertNotIn("response_format", options)
                    return model_call(action)
                self.assertIsNone(schema)
                self.assertEqual(options["response_format"]["type"], "json_schema")
                self.assertTrue(options["response_format"]["json_schema"]["strict"])
                self.assertEqual(len(messages), 2)
                self.assertEqual(json.loads(messages[1]["content"])["articles"][1]["processing_status"], "analyzed")
                row = session.results["study_fresh"]["results"][1]
                chart = session.results["chart_cs"]
                facts = [
                    {"language": "pl", "resolution_status": "no_sitelink", "collection_status": "not_collected",
                     "baseline_coverage_status": None, "current_coverage_status": None,
                     "baseline_mean_daily_views_observed": None, "current_mean_daily_views_observed": None,
                     "current_missing_days": None, "change_percent": None, "change_reason": "no_sitelink",
                     "source_url": None, "chart_path": None, "descriptive_seasonality_adjusted": None,
                     "trend_model_status": None, "trend_calendar_controls": [],
                     "trend_interval_status": None, "trend_interval_reason": None},
                    {"language": "cs", "resolution_status": "matched", "collection_status": "analyzed",
                     "baseline_coverage_status": "complete", "current_coverage_status": "partial",
                     "baseline_mean_daily_views_observed": 10.0, "current_mean_daily_views_observed": 20.0,
                     "current_missing_days": 1, "change_percent": None, "change_reason": "incomplete_coverage",
                     "source_url": row["source"]["url"], "chart_path": chart["artifacts"]["chart"],
                     "descriptive_seasonality_adjusted": False, "trend_model_status": "not_computed",
                     "trend_calendar_controls": ["weekday", "month_of_year"],
                     "trend_interval_status": "not_computed", "trend_interval_reason": "model_not_fitted"},
                ]
                answer = {"facts": facts, "summary_uk": "Дані неповні; повну зміну не обчислено.",
                          "limitations_uk": ["Перегляди не доводять попиту."], "next_steps_uk": ["Перевірити пропуски."]}
                return {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(model_fields(answer)), "tool_calls": []}}

            report = run_session(session, complete, "Skill instructions")
            self.assertEqual(report["status"], "completed", report.get("error"))
            self.assertEqual(report["model_calls"], 5)
            self.assertTrue(report["facts_verified"])
            self.assertTrue(report["narrative_review_required"])
            self.assertEqual(report["answer_contract_version"], 4)
            self.assertEqual(report["host_bound_fields"], ["source_url", "chart_path"])
            self.assertEqual(report["host_rendered_fields"], ["summary_uk", "limitations_uk", "next_steps_uk"])
            self.assertEqual(report["final_request"]["response_format"]["type"], "json_schema")
            self.assertTrue(session.results["study_offline"]["results"][1]["cache_hit"])
            self.assertEqual(session.results["analyze_cs"]["methodology"]["trend_model"]["reason"], "incomplete_coverage")
            png = Path(session.results["chart_cs"]["artifacts"]["chart"])
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            modified = report["answer"]
            modified["facts"][1]["change_percent"] = 100.0
            with self.assertRaises(PageviewsError) as caught:
                session.finish(json.dumps(model_fields(modified)))
            self.assertEqual(caught.exception.code, "live_fact_mismatch")
            with self.assertRaises(PageviewsError):
                session.execute({"operation": "chart", "language": "pl"})
            self.assertEqual(len(requests), 5)
        network.assert_called_once()