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

from tools.model_eval.cases import prepare_cases
from tools.model_eval.cli import main
from tools.model_eval.client import ModelClient, validate_options
from tools.model_eval.config import ModelConfig
from tools.pageviews.errors import PageviewsError
from tools.request_metrics import capture_request_metrics


def completion_payload(content=None, calls=None, model="qwen3-coder-next"):
    return {
        "id": "fixture-completion", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{
            "index": 0, "finish_reason": "tool_calls" if calls else "stop",
            "message": {"role": "assistant", "content": content, "tool_calls": calls},
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
    }


class ModelEvaluationCoreTests(unittest.TestCase):
    def test_help_does_not_require_sdk_statistics_or_credentials(self):
        completed = subprocess.run(
            [sys.executable, "-S", "-m", "tools.model_eval", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--dry-run", completed.stdout)
        self.assertIn("--model", completed.stdout)
        self.assertNotIn("--api-key", completed.stdout)

    def test_invalid_resource_limits_fail_before_any_client_is_created(self):
        for timeout, tokens in ((0, 1200), (float("nan"), 1200), (60, 0), (60, 4097), (60, True)):
            with self.subTest(timeout=timeout, tokens=tokens):
                with self.assertRaises(PageviewsError) as caught:
                    validate_options(timeout, tokens)
                self.assertEqual(caught.exception.code, "invalid_request")


@unittest.skipUnless(find_spec("openai") is not None, "Optional evaluation extra is not installed")
class ModelClientTests(unittest.TestCase):
    def setUp(self):
        import httpx2

        self.http = httpx2
        self.config = ModelConfig("https://models.example/v1", "qwen3-coder-next", "fixture-api-key")

    def test_custom_provider_auth_and_budget_without_other_account_headers(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(200, json=completion_payload("{}"))

        with patch.dict(os.environ, {"OPENAI_ORG_ID": "other-org", "OPENAI_PROJECT_ID": "other-project"}):
            with ModelClient(self.config, transport=self.http.MockTransport(handle), max_tokens=321) as client:
                result = client.complete([{"role": "user", "content": "Fixture"}], None)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://models.example/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer fixture-api-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "qwen3-coder-next")
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("fixture-api-key", request.content.decode())
        self.assertNotIn("other-org", str(request.headers))
        self.assertNotIn("other-project", str(request.headers))
        self.assertEqual(result["usage"]["total_tokens"], 200)
        self.assertEqual(result["message"]["content"], "{}")

    def test_provider_failures_are_not_retried_or_echoed(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                requests = []

                def handle(request):
                    requests.append(request)
                    return self.http.Response(status, json={"error": {"message": "fixture-api-key"}})

                with capture_request_metrics() as metrics:
                    with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
                        with self.assertRaises(PageviewsError) as caught:
                            client.complete([{"role": "user", "content": "Fixture"}], None)
                self.assertEqual(caught.exception.code, "model_http_error")
                self.assertEqual(caught.exception.details["http_status"], status)
                self.assertNotIn("fixture-api-key", json.dumps(caught.exception.as_dict()))
                self.assertEqual(len(requests), 1)
                measured = metrics.snapshot()["http"]["model"]
                self.assertEqual(measured["attempts"], 1)
                self.assertEqual(measured["errors"], 1)
                self.assertEqual(measured["status_codes"], {str(status): 1})

    def test_malformed_success_response_is_a_structured_error_without_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(200, content=b"{malformed", headers={"content-type": "application/json"})

        with capture_request_metrics() as metrics:
            with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
                with self.assertRaises(PageviewsError) as caught:
                    client.complete([{"role": "user", "content": "Fixture"}], None)
        self.assertEqual(caught.exception.code, "model_response_error")
        self.assertNotIn("malformed", str(caught.exception))
        self.assertEqual(len(requests), 1)

    def test_final_response_uses_native_json_schema_without_tools(self):
        requests = []
        response_format = {"type": "json_schema", "json_schema": {
            "name": "test_answer", "strict": True,
            "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"], "additionalProperties": False},
        }}

        def handle(request):
            requests.append(json.loads(request.content))
            return self.http.Response(200, json=completion_payload('{"text":"Відповідь"}'))

        with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
            result = client.complete(
                [{"role": "user", "content": "Final answer"}], None,
                response_format=response_format,
            )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["response_format"], response_format)
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("tool_choice", requests[0])
        self.assertEqual(result["message"]["content"], '{"text":"Відповідь"}')

    def test_schema_rejection_is_not_silently_retried_as_plain_text(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(400, json={"error": {"message": "Unsupported response_format"}})

        with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
            with self.assertRaises(PageviewsError) as caught:
                client.complete([], None, response_format={"type": "json_schema"})
        self.assertEqual(caught.exception.code, "model_http_error")
        self.assertEqual(caught.exception.details["http_status"], 400)
        self.assertEqual(len(requests), 1)

    def test_structured_refusal_is_preserved_for_final_validation(self):
        payload = completion_payload('{"text":"not an accepted answer"}')
        payload["choices"][0]["message"]["refusal"] = "Unable to answer"
        transport = self.http.MockTransport(lambda request: self.http.Response(200, json=payload))
        with ModelClient(self.config, transport=transport) as client:
            result = client.complete([], None)
        self.assertEqual(result["message"]["refusal"], "Unable to answer")

    def test_redirects_cannot_move_authenticated_requests_to_another_host(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(307, headers={"Location": "https://elsewhere.example/v1/chat/completions"})

        with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
            with self.assertRaises(PageviewsError):
                client.complete([{"role": "user", "content": "Fixture"}], None)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "models.example")

    def test_timeout_is_a_single_attempt_without_raw_exception_text(self):
        requests = []

        def handle(request):
            requests.append(request)
            raise self.http.ReadTimeout("fixture-api-key", request=request)

        with capture_request_metrics() as metrics:
            with ModelClient(self.config, transport=self.http.MockTransport(handle)) as client:
                with self.assertRaises(PageviewsError) as caught:
                    client.complete([{"role": "user", "content": "Fixture"}], None)
        self.assertEqual(caught.exception.code, "model_connection_error")
        self.assertNotIn("fixture-api-key", str(caught.exception))
        self.assertEqual(len(requests), 1)
        measured = metrics.snapshot()["http"]["model"]
        self.assertEqual(measured["attempts"], 1)
        self.assertEqual(measured["errors"], 1)
        self.assertEqual(measured["status_codes"], {})

    def test_unusable_or_sensitive_model_responses_are_not_recorded(self):
        for payload in (
            {"choices": []}, completion_payload("fixture-api-key"),
            {**completion_payload("{}"), "usage": {"total_tokens": -1}},
        ):
            with self.subTest(payload=payload):
                transport = self.http.MockTransport(lambda request: self.http.Response(200, json=payload))
                with ModelClient(self.config, transport=transport) as client:
                    with self.assertRaises(PageviewsError) as caught:
                        client.complete([{"role": "user", "content": "Fixture"}], None)
                self.assertEqual(caught.exception.code, "model_response_error")
                self.assertNotIn("fixture-api-key", str(caught.exception))


@unittest.skipUnless(find_spec("statsmodels") is not None, "Optional statistics extra is not installed")
class EvaluationCasesTests(unittest.TestCase):
    def test_worked_cases_distinguish_missing_calendar_and_linear_evidence(self):
        cases = {case.identifier: case for case in prepare_cases()}
        missing, calendar, trend = (cases[name] for name in ("missing_day", "calendar_pattern", "linear_trend"))
        self.assertIsNone(missing.expected["observed_change_percent"])
        self.assertEqual(missing.expected["observed_change_reason"], "incomplete_coverage")
        self.assertEqual(missing.expected["interval_target"], "unavailable")
        self.assertAlmostEqual(calendar.expected["slope_daily_views_per_year"], 0, places=8)
        self.assertTrue(calendar.expected["interval_includes_zero"])
        self.assertAlmostEqual(trend.expected["slope_daily_views_per_year"], 365.25, places=8)
        self.assertFalse(trend.expected["interval_includes_zero"])
        for case in cases.values():
            self.assertFalse(case.expected["future_growth_established"])
            self.assertFalse(case.expected["product_demand_established"])
            self.assertNotIn("expected", case.evidence)
            self.assertEqual(case.evidence["data_kind"], "synthetic_not_wikimedia_observations")


@unittest.skipUnless(
    find_spec("statsmodels") is not None and find_spec("openai") is not None and find_spec("dotenv") is not None,
    "Optional statistics and evaluation extras are not installed",
)
class ModelEvaluationCliTests(unittest.TestCase):
    def setUp(self):
        import httpx2

        self.http = httpx2
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root_patch = patch("tools.model_eval.cli.ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        (self.root / "SKILL.md").write_text("## Analytical methodology\nFixture instructions.\n## Other\nNot included.")
        self.env = self.root / ".env"
        self.env.write_text(
            "TREND_VISOR_LLM_BASE_URL=https://models.example/v1\n"
            "TREND_VISOR_LLM_MODEL=qwen3-coder-next\n"
            "TREND_VISOR_LLM_API_KEY=fixture-api-key\n",
        )

    def invoke(self, *arguments):
        stream = io.StringIO()
        with redirect_stdout(stream), patch.dict(os.environ, {}, clear=True):
            code = main(list(arguments))
        return code, json.loads(stream.getvalue())

    def test_dry_run_never_loads_credentials_or_creates_a_model_client(self):
        with patch("tools.model_eval.cli.load_config", side_effect=AssertionError("No credentials")):
            with patch("tools.model_eval.cli.ModelClient", side_effect=AssertionError("No HTTP")):
                code, result = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "prepared")
        self.assertFalse(result["model_test_performed"])
        self.assertEqual(result["summary"]["model_calls"], 0)
        report = json.loads(Path(result["artifacts"]["evaluation"]).read_text())
        self.assertEqual(len(report["results"]), 3)
        self.assertNotIn("Not included.", report["instructions"])

    def test_cli_roundtrip_saves_evidence_responses_and_model_override(self):
        expected = {case.identifier: case.expected for case in prepare_cases()}
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body["model"], "gemma4")
            if body["messages"][-1]["role"] == "user":
                identifier = body["messages"][-1]["content"].splitlines()[0].split(": ")[1]
                calls = [{"id": "call-fixture", "type": "function", "function": {
                    "name": "analyze_case", "arguments": json.dumps({"case_id": identifier}),
                }}]
                payload = completion_payload(calls=calls, model="gemma4")
            else:
                evidence = json.loads(body["messages"][-1]["content"])
                payload = completion_payload(json.dumps(expected[evidence["case_id"]]), model="gemma4")
            return self.http.Response(200, json=payload)

        def client(config, **options):
            return ModelClient(config, transport=self.http.MockTransport(handle), **options)

        with patch("tools.model_eval.cli.ModelClient", side_effect=client):
            code, result = self.invoke("--model", "gemma4")
        self.assertEqual(code, 0)
        self.assertEqual(result["summary"]["passed"], 3)
        self.assertEqual(result["summary"]["model_calls"], 6)
        self.assertEqual(len(requests), 6)
        report_text = Path(result["artifacts"]["evaluation"]).read_text()
        report = json.loads(report_text)
        self.assertEqual(report["model"], "gemma4")
        self.assertEqual(report["results"][0]["responses"][0]["usage"]["total_tokens"], 200)
        self.assertNotIn("fixture-api-key", report_text)

    def test_provider_error_stops_remaining_cases_and_saves_partial_report(self):
        requests = []

        def handle(request):
            requests.append(request)
            return self.http.Response(401, json={"error": {"message": "fixture-api-key"}})

        def client(config, **options):
            return ModelClient(config, transport=self.http.MockTransport(handle), **options)

        with patch("tools.model_eval.cli.ModelClient", side_effect=client):
            code, result = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(result["summary"], {"cases": 3, "passed": 0, "failed": 1, "not_run": 2, "model_calls": 1})
        self.assertEqual(len(requests), 1)
        text = Path(result["artifacts"]["evaluation"]).read_text()
        self.assertNotIn("fixture-api-key", text)

    def test_existing_output_and_invalid_options_cannot_trigger_http(self):
        destination = self.root / "previous.json"
        destination.write_bytes(b"previous evaluation")
        with patch("tools.model_eval.cli.ModelClient", side_effect=AssertionError("No HTTP")):
            code, result = self.invoke("--output", str(destination))
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "output_exists")
            code, result = self.invoke("--timeout", "nan")
            self.assertEqual(code, 2)
            self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(destination.read_bytes(), b"previous evaluation")