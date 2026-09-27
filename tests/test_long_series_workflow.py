import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tests.report_helpers import saved_study
from tests.test_discovery import QUESTION
from tests.test_workflow import model_action
from tools.model_eval.workflow_runner import load_instructions, run_workflow
from tools.model_eval.workflow_tools import WorkflowAdapter
from tools.pageviews.artifacts import MAX_ARTIFACT_BYTES, save_json_artifact
from tools.pageviews.client import MAX_RESPONSE_BYTES, fetch_url
from tools.pageviews.errors import PageviewsError
from tools.pageviews.methodology import MethodologyOptions
from tools.pageviews.research_state import create_research


YEARS = (1, 2, 5, 8)
LANGUAGES = ("cs", "pl", "en")
OBSERVATION_PAGE_SIZE = 50
MISSING_PAGE_SIZE = 10
MONTH_PAGE_SIZE = 12
CALENDAR_PAGE_SIZE = 8


def _series(years: int, language_index: int) -> tuple[int | None, ...]:
    start = date(2018, 1, 1)
    count = (date(start.year + years, 1, 1) - start).days
    values = []
    for day in range(count):
        if day % 503 == 17 + language_index:
            values.append(None)
        elif day % 97 == 31 + language_index:
            values.append(5000 + language_index * 100)
        elif day % 19 == 0:
            values.append(0)
        else:
            values.append(10 + (day * 7 + language_index * 13) % 100)
    return tuple(values)


def run_scale_benchmark() -> list[dict]:
    """Run the same offline multi-language callback flow for 1/2/5/8 years."""
    measurements = []
    with patch("tools.pageviews.client.urlopen", side_effect=AssertionError("Long-series benchmark must not use HTTP")):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for years in YEARS:
                fixture_root = root / f"{years}-years"
                values = {language: _series(years, index) for index, language in enumerate(LANGUAGES)}
                count = len(values[LANGUAGES[0]])
                split = count // 2
                first_day = date(2018, 1, 1)
                last_day = first_day.fromordinal(first_day.toordinal() + count - 1)
                study_ref, study = saved_study(
                    fixture_root,
                    values,
                    unmatched=(),
                    start=first_day,
                    split=split,
                    monthly=True,
                    methodology=MethodologyOptions(),
                )
                research = create_research(
                    study_ref, question=f"Synthetic {years}-year trend comparison",
                    output=fixture_root / "research.json",
                )
                adapter = WorkflowAdapter("research", research, fixture_root / "run")
                start_text = study["periods"]["baseline"]["start"]
                end_text = study["periods"]["current"]["end"]
                actions = [
                    model_action("evidence"),
                    model_action("evidence"),
                    model_action("detail", language="cs", detail_kind="observations",
                                 start=start_text, end=end_text, offset=0, limit=OBSERVATION_PAGE_SIZE),
                    model_action("detail", language="cs", detail_kind="observations",
                                 start=start_text, end=end_text, offset=OBSERVATION_PAGE_SIZE,
                                 limit=OBSERVATION_PAGE_SIZE),
                    model_action("detail", language="cs", detail_kind="missing_dates",
                                 start=start_text, end=end_text, offset=0, limit=MISSING_PAGE_SIZE),
                    model_action("detail", language="cs", detail_kind="monthly_summaries",
                                 offset=0, limit=MONTH_PAGE_SIZE),
                    model_action("detail", language="cs", detail_kind="calendar_comparisons",
                                 offset=0, limit=CALENDAR_PAGE_SIZE),
                    model_action("report"),
                ]
                prompt_requests = []
                responses = iter(actions)

                def complete(messages, tool):
                    prompt_requests.append((messages, tool))
                    return next(responses)

                result = run_workflow(
                    adapter,
                    complete,
                    lambda current: load_instructions(Path(__file__).resolve().parents[1], current),
                    max_calls=8,
                )
                if result["status"] != "completed":
                    raise AssertionError(result.get("error", result["status"]))
                if len(prompt_requests) != 8:
                    raise AssertionError(f"Expected 8 model callbacks, got {len(prompt_requests)}")

                # Independent arithmetic oracle: verify whole-period observed sums
                # from the synthetic input, including unknowns and explicit zeros.
                for index, language in enumerate(LANGUAGES):
                    row = study["results"][index]
                    raw_values = values[language]
                    expected_baseline = sum(value for value in raw_values[:split] if value is not None)
                    expected_current = sum(value for value in raw_values[split:] if value is not None)
                    if row["analysis"]["baseline"]["sum_observed_views"] != expected_baseline:
                        raise AssertionError(f"Baseline sum mismatch for {years}y/{language}")
                    if row["analysis"]["current"]["sum_observed_views"] != expected_current:
                        raise AssertionError(f"Current sum mismatch for {years}y/{language}")

                prompt_bytes = [event["context_preflight"]["input_utf8_bytes"]
                                for event in result["events"]]
                result_bytes = [Path(event["result"]["path"]).stat().st_size
                                for event in result["events"]]
                if any(value > MAX_ARTIFACT_BYTES for value in prompt_bytes + result_bytes):
                    raise AssertionError("A request/result artifact exceeded the 10 MiB cap")
                if any(event["context_preflight"]["soft_guideline_exceeded"] for event in result["events"]):
                    # May exceed 10K due to mandatory references; this is measured,
                    # never a rejection or a test failure.
                    soft_exceeded = True
                else:
                    soft_exceeded = False
                observations = [json.loads(Path(event["result"]["path"]).read_bytes())
                                for event in result["events"] if event["result"]]
                daily_pages = [item for item in observations
                               if item.get("operation") == "evidence_detail" and item.get("kind") == "observations"]
                if len(daily_pages) != 2 or any(len(item["items"]) > OBSERVATION_PAGE_SIZE for item in daily_pages):
                    raise AssertionError("Daily observations were not bounded/paginated")

                snapshot_bytes = sum(
                    Path(row["artifacts"][key]).stat().st_size
                    for row in study["results"] if row["status"] == "analyzed"
                    for key in ("raw", "series", "metadata")
                )
                run_bytes = sum(path.stat().st_size for path in (fixture_root / "run").rglob("*") if path.is_file())
                report_path = Path(result["report_artifacts"]["report"])
                markdown_path = Path(result["report_artifacts"]["markdown"])
                measurements.append({
                    "years": years,
                    "days_per_language": count,
                    "languages": len(LANGUAGES),
                    "daily_rows_processed": count * len(LANGUAGES),
                    "model_callbacks": result["model_calls"],
                    "max_request_estimate_bytes": max(prompt_bytes),
                    "sum_request_estimate_bytes": sum(prompt_bytes),
                    "max_result_artifact_bytes": max(result_bytes),
                    "snapshot_artifact_bytes": snapshot_bytes,
                    "workflow_artifact_bytes": run_bytes,
                    "report_json_bytes": report_path.stat().st_size,
                    "report_markdown_bytes": markdown_path.stat().st_size,
                    "workflow_elapsed_seconds": result["elapsed_seconds"],
                    "soft_10000_exceeded_any": soft_exceeded,
                    "wikimedia_http_attempts": result["request_metrics"]["http"]["pageviews"]["attempts"],
                    "model_usage_complete": result["usage_summary"]["complete"],
                })
    return measurements


class LongSeriesWorkflowTests(unittest.TestCase):
    def test_1_2_5_8_year_multi_language_followups_stay_bounded_and_offline(self):
        measurements = run_scale_benchmark()
        self.assertEqual([item["years"] for item in measurements], [1, 2, 5, 8])
        self.assertTrue(all(item["languages"] == 3 for item in measurements))
        self.assertTrue(all(item["model_callbacks"] == 8 for item in measurements))
        self.assertTrue(all(item["wikimedia_http_attempts"] == 0 for item in measurements))
        self.assertTrue(all(item["model_usage_complete"] is False for item in measurements))
        self.assertTrue(all(item["max_request_estimate_bytes"] < MAX_ARTIFACT_BYTES for item in measurements))
        self.assertTrue(all(item["max_result_artifact_bytes"] < MAX_ARTIFACT_BYTES for item in measurements))
        # Daily volume grows 8x, but repeated prompt footprint is state/pages, not raw history.
        self.assertLess(measurements[-1]["max_request_estimate_bytes"] - measurements[0]["max_request_estimate_bytes"], 5000)
        self.assertGreater(measurements[-1]["snapshot_artifact_bytes"], measurements[0]["snapshot_artifact_bytes"])

    @patch("tools.pageviews.client.urlopen")
    def test_real_10_mib_response_boundary_accepts_cap_and_rejects_one_byte_over(self, opener):
        at_cap = MagicMock(status=200)
        at_cap.read.return_value = b"x" * MAX_RESPONSE_BYTES
        at_cap.__enter__.return_value = at_cap
        over_cap = MagicMock(status=200)
        over_cap.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
        over_cap.__enter__.return_value = over_cap
        opener.side_effect = [at_cap, over_cap]

        accepted = fetch_url("https://metrics.example/data", user_agent="limit-test/1")
        self.assertEqual(len(accepted.body), MAX_RESPONSE_BYTES)
        with self.assertRaises(PageviewsError) as caught:
            fetch_url("https://metrics.example/data", user_agent="limit-test/1")
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertEqual(opener.call_count, 2)

    def test_real_10_mib_json_artifact_boundary_accepts_cap_and_rejects_one_byte_over(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            empty = (json.dumps({"payload": ""}, ensure_ascii=False, sort_keys=True,
                                indent=2, allow_nan=False) + "\n").encode("utf-8")
            fixed_bytes = len(empty)
            exact_payload = "x" * (MAX_ARTIFACT_BYTES - fixed_bytes)
            saved = save_json_artifact({"payload": exact_payload}, root / "at-cap.json")
            self.assertEqual(saved.path.stat().st_size, MAX_ARTIFACT_BYTES)
            with self.assertRaises(PageviewsError) as caught:
                save_json_artifact({"payload": exact_payload + "x"}, root / "over-cap.json")
            self.assertEqual(caught.exception.code, "artifact_too_large")
            self.assertFalse((root / "over-cap.json").exists())


if __name__ == "__main__":
    print(json.dumps(run_scale_benchmark(), ensure_ascii=False, indent=2, allow_nan=False))
