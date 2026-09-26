import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.final_answer_helpers import model_fields
from tests.replay_helpers import replay_answer, saved_research
from tools.model_eval.client import ModelClient
from tools.model_eval.final_replay import load_research, main, run_replay
from tools.pageviews.errors import PageviewsError


class FinalReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path, self.checksum, self.original = saved_research(self.root)

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Wikimedia must remain offline"))
    def test_one_final_call_reuses_pinned_evidence_and_preserves_old_failure(self, network):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        saved = load_research(self.path, self.checksum)
        requests = []
        answer = replay_answer(self.original)

        def complete(messages, tool, *, response_format):
            requests.append(messages)
            self.assertIsNone(tool)
            self.assertEqual(response_format["type"], "json_schema")
            self.assertNotIn("PREVIOUS WRONG ANSWER", json.dumps(messages))
            self.assertNotIn('"expected"', json.dumps(messages))
            return {"finish_reason": "stop", "message": {"content": json.dumps(model_fields(answer))}}

        result = run_replay(saved, complete, "Updated full skill")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["wikimedia_requests"], 0)
        self.assertEqual(result["source_live"]["sha256"], self.checksum)
        self.assertEqual(result["source_live"]["status"], "failed")
        self.assertTrue(result["facts_verified"])
        self.assertTrue(result["narrative_review_required"])
        self.assertEqual(result["answer"]["facts"], answer["facts"])
        self.assertIn("зросли з 15,00 до 45,00 на день", result["answer"]["summary_uk"])
        self.assertEqual(result["host_rendered_fields"], ["summary_uk", "limitations_uk", "next_steps_uk"])
        self.assertEqual(result["host_bound_fields"], ["source_url", "chart_path"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        network.assert_not_called()

    def test_invalid_checkpoint_checksum_blocks_loading(self):
        for checksum in ("0" * 64, "wrong", "é" * 64, None):
            with self.subTest(checksum=checksum):
                with self.assertRaises(PageviewsError):
                    load_research(self.path, checksum)

    def test_changed_snapshot_operation_or_chart_blocks_replay(self):
        files = (
            Path(self.original["operation_results"]["study_fresh"]["results"][0]["artifacts"]["raw"]),
            self.path.parent / "analyze_uk-result.json",
            self.path.parent / "uk-daily.png",
        )
        for path in files:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(b"damaged fixture")
                with self.assertRaises(PageviewsError):
                    load_research(self.path, self.checksum)
                path.write_bytes(original)

    def test_inconsistent_offline_result_is_rejected_even_with_new_file_hash(self):
        changed = self.original
        changed["operation_results"]["study_offline"]["results"][0]["cache_hit"] = False
        self.path.write_text(json.dumps(changed))
        (self.path.parent / "study_offline-result.json").write_text(json.dumps(changed["operation_results"]["study_offline"]))
        with self.assertRaises(PageviewsError):
            load_research(self.path, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_fenced_or_incorrect_answer_remains_a_failed_record_without_retry(self):
        saved = load_research(self.path, self.checksum)
        bad = replay_answer(self.original)
        bad["facts"][0]["collection_status"] = "complete"
        for content in (json.dumps(model_fields(bad)), "```json\n" + json.dumps(model_fields(replay_answer(self.original))) + "\n```"):
            with self.subTest(content=content[:10]):
                count = []

                def complete(messages, tool, **options):
                    count.append(1)
                    return {"finish_reason": "stop", "message": {"content": content}}

                result = run_replay(saved, complete, "Skill")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["response"]["message"]["content"], content)
                self.assertEqual(len(count), 1)
                self.assertNotIn("answer", result)

    def test_provider_failure_is_recorded_without_fallback(self):
        saved = load_research(self.path, self.checksum)

        def complete(messages, tool, **options):
            raise PageviewsError("model_http_error", "Unsupported structured response", details={"http_status": 400})

        result = run_replay(saved, complete, "Skill")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["error"]["details"]["http_status"], 400)

    def test_help_requires_no_optional_packages_or_credentials(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "tools.model_eval.final_replay", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--live-sha256", result.stdout)
        self.assertNotIn("--refresh", result.stdout)


@unittest.skipUnless(
    find_spec("openai") is not None and find_spec("dotenv") is not None,
    "Optional evaluation extra is not installed",
)
class FinalReplayCliTests(unittest.TestCase):
    def setUp(self):
        import httpx2

        self.http = httpx2
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path, self.checksum, self.original = saved_research(self.root)
        (self.root / "SKILL.md").write_text("# Updated instructions\nKeep the analysis scopes separate.")
        (self.root / ".env").write_text(
            "TREND_VISOR_LLM_BASE_URL=https://models.example/v1\n"
            "TREND_VISOR_LLM_MODEL=qwen3-coder-next\n"
            "TREND_VISOR_LLM_API_KEY=fixture-local-key\n",
        )
        root_patch = patch("tools.model_eval.final_replay.ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output), patch.dict(os.environ, {}, clear=True):
            code = main(["--live", str(self.path), "--live-sha256", self.checksum, *extra])
        return code, json.loads(output.getvalue())

    @patch("tools.pageviews.client.urlopen", side_effect=AssertionError("No Wikimedia access"))
    def test_cli_saves_one_structured_response_without_changing_saved_evidence(self, network):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        requests = []
        answer = replay_answer(self.original)

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            self.assertNotIn("tools", body)
            self.assertNotIn("fixture-local-key", request.content.decode())
            self.assertNotIn("PREVIOUS WRONG ANSWER", request.content.decode())
            return self.http.Response(200, json={
                "id": "fixture-final", "object": "chat.completion", "created": 1, "model": "qwen3-coder-next",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(model_fields(answer)),
                }}],
            })

        def client(config, **options):
            return ModelClient(config, transport=self.http.MockTransport(handle), **options)

        with patch("tools.model_eval.final_replay.ModelClient", side_effect=client):
            code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["wikimedia_requests"], 0)
        self.assertEqual(len(requests), 1)
        artifact = Path(result["artifacts"]["replay"])
        text = artifact.read_text()
        report = json.loads(text)
        self.assertEqual(report["source_live"]["status"], "failed")
        self.assertEqual(report["answer_contract_version"], 4)
        self.assertEqual(report["host_bound_fields"], ["source_url", "chart_path"])
        self.assertEqual(report["host_rendered_fields"], ["summary_uk", "limitations_uk", "next_steps_uk"])
        self.assertNotIn("summary_uk", json.loads(report["response"]["message"]["content"]))
        self.assertNotIn("chart_path", json.loads(report["response"]["message"]["content"])["facts"][0])
        self.assertTrue(report["narrative_review_required"])
        self.assertFalse(report["original_test_regraded"])
        self.assertEqual(report["request"]["messages"], requests[0]["messages"])
        self.assertEqual(result["artifacts"]["replay_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertNotIn("fixture-local-key", text)
        self.assertTrue(all(path.read_bytes() == body for path, body in before.items()))
        network.assert_not_called()

    def test_existing_output_or_changed_source_blocks_sdk_construction(self):
        output = self.root / "old.json"
        output.write_bytes(b"previous result")
        with patch("tools.model_eval.final_replay.ModelClient", side_effect=AssertionError("No model call")):
            code, result = self.invoke("--output", str(output))
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "output_exists")
            code, result = self.invoke("--live-sha256", "0" * 64)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "replay_source_error")
        self.assertEqual(output.read_bytes(), b"previous result")

    def test_schema_rejection_is_saved_without_fallback_or_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(400, json={"error": {"message": "Unsupported response_format"}})

        def client(config, **options):
            return ModelClient(config, transport=self.http.MockTransport(handle), **options)

        with patch("tools.model_eval.final_replay.ModelClient", side_effect=client):
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["details"]["http_status"], 400)
        self.assertEqual(len(requests), 1)
        self.assertTrue(Path(result["artifacts"]["replay"]).is_file())