import base64
import hashlib
import json
from copy import deepcopy
from datetime import date

from tests.helpers import encode_items, make_item, make_request
from tools.pageviews.analysis import Period, analyze_series
from tools.pageviews.diagnostics import run_diagnostics
from tools.pageviews.methodology import assess_methodology
from tools.pageviews.models import RawResponse
from tools.pageviews.storage import save_snapshot
from tools.pageviews.validation import validate_response


def saved_research(root):
    request = make_request(
        project="uk.wikipedia.org", article="Synthetic article",
        start="2026-01-01", end="2026-01-04", as_of="2026-02-01",
    )
    body = encode_items(*(
        make_item(f"2026010{day}00", value, project=request.project, article=request.article)
        for day, value in enumerate((10, 20, 30, 60), start=1)
    ))
    response = RawResponse(request.url, "2026-02-01T08:00:00+00:00", body)
    series = validate_response(body, request)
    snapshot = save_snapshot(root / "cache", request, response, series)
    baseline, current = Period(date(2026, 1, 1), date(2026, 1, 2)), Period(date(2026, 1, 3), date(2026, 1, 4))
    analysis = analyze_series(series, baseline, current)
    analysis["methodology"] = assess_methodology(series, baseline, current)
    scope = {
        "topic": "synthetic topic", "question": "Чи зростає увага до цієї статті?", "languages": ["uk"],
        "periods": {"baseline-start": "2026-01-01", "baseline-end": "2026-01-02",
                    "current-start": "2026-01-03", "current-end": "2026-01-04"},
        "as_of": "2026-02-01", "phase": "research",
    }
    row = {
        "language": "uk", "article": "Synthetic article", "project": request.project,
        "status": "analyzed", "resolution_status": "matched", "reason": None, "cache_hit": False,
        "snapshot": str(snapshot.directory), "request": request.as_dict(), "source": response.source_metadata(),
        "artifacts": snapshot.artifact_paths(), "coverage": series.coverage_summary(), "analysis": analysis,
    }
    fresh = {"operation": "study", "results": [row], "comparison": {"status": "not_computed"},
             "periods": {"baseline": analysis["baseline"]["window"], "current": analysis["current"]["window"]},
             "as_of": scope["as_of"]}
    offline = deepcopy(fresh)
    offline["results"][0]["cache_hit"] = True
    directory = root / "run"
    directory.mkdir()
    png = directory / "uk-daily.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGocAAAAASUVORK5CYII="
    ))
    chart = {
        "operation": "chart", "snapshot": row["snapshot"], "request": row["request"], "source": row["source"],
        "periods": {key: analysis[key] for key in ("baseline", "current")},
        "artifacts": {"chart": str(png)}, "chart_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
    }
    detailed = {**deepcopy(analysis), "operation": "analyze", "snapshot": row["snapshot"],
                "request": row["request"], "source": row["source"], "artifacts": row["artifacts"],
                "diagnostics": run_diagnostics(series, baseline, current)}
    operations = {"study_fresh": fresh, "study_offline": offline, "analyze_uk": detailed, "chart_uk": chart}
    report = {
        "live_evaluation_version": 1, "phase": "research", "status": "failed", "scope": scope,
        "model": "fixture-model", "operation_results": operations,
        "events": [{"response": {"message": {"content": "PREVIOUS WRONG ANSWER MUST NOT BE REPLAYED"}}}],
    }
    for name, value in operations.items():
        (directory / f"{name}-result.json").write_text(json.dumps(value), encoding="utf-8")
    path = directory / "live.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), report


def replay_answer(report):
    row = report["operation_results"]["study_fresh"]["results"][0]
    return {
        "facts": [{
            "language": "uk", "resolution_status": "matched", "collection_status": "analyzed",
            "baseline_coverage_status": "complete", "current_coverage_status": "complete",
            "baseline_mean_daily_views_observed": 15.0, "current_mean_daily_views_observed": 45.0,
            "current_missing_days": 0, "change_percent": 200.0, "change_reason": None,
            "source_url": row["source"]["url"],
            "chart_path": report["operation_results"]["chart_uk"]["artifacts"]["chart"],
            "descriptive_seasonality_adjusted": False, "trend_model_status": "not_requested",
            "trend_calendar_controls": [], "trend_interval_status": None, "trend_interval_reason": None,
        }],
        "summary_uk": "Середні перегляди зросли з 15 до 45 за день у вибраних періодах.",
        "limitations_uk": ["Перегляди однієї статті не доводять попиту на курс."],
        "next_steps_uk": ["Перевірити інтерес користувачів застосунку окремо."],
    }