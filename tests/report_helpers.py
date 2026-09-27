import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta

from tests.helpers import encode_items, make_item, make_request
from tests.study_helpers import TITLES, resolution_result
from tools.pageviews.analysis import Period
from tools.pageviews.analysis_cli import main as analyze_main
from tools.pageviews.artifacts import save_json_artifact
from tools.pageviews.chart_cli import main as chart_main
from tools.pageviews.models import RawResponse
from tools.pageviews.resolutions import read_resolution, save_resolution
from tools.pageviews.storage import save_snapshot
from tools.pageviews.studies import run_study
from tools.pageviews.validation import validate_response


def saved_study(root, values=None, *, unmatched=("en",), start=date(2026, 7, 1),
                split=2, monthly=False, methodology=None, uncached=()):
    values = {"cs": (10, 20, 30, 60), "pl": (100, 200, 200, 400)} if values is None else values
    languages = (*values, *uncached, *unmatched)
    saved = save_resolution(resolution_result(languages, unmatched), root / "resolution.json")
    plan = read_resolution(saved.path, saved.sha256)
    count = len(next(iter(values.values()))) if values else 4
    end = start + timedelta(days=count - 1)
    as_of = (end + timedelta(days=8)).isoformat()
    for language, observations in values.items():
        request = make_request(project=f"{language}.wikipedia.org", article=TITLES[language],
                               start=start.isoformat(), end=end.isoformat(), as_of=as_of)
        body = encode_items(*(
            make_item((start + timedelta(days=offset)).strftime("%Y%m%d00"), views,
                      project=request.project, article=request.article)
            for offset, views in enumerate(observations) if views is not None
        ))
        response = RawResponse(request.url, as_of + "T08:00:00+00:00", body)
        save_snapshot(root / "cache", request, response, validate_response(body, request))
    study = run_study(
        plan, Period(start, start + timedelta(days=split - 1)),
        Period(start + timedelta(days=split), end),
        as_of=as_of, cache_dir=root / "cache", offline=True,
        include_monthly=monthly, methodology=methodology,
    )
    artifact = save_json_artifact(study, root / "study.json")
    return artifact, study


def saved_analysis(root, study, *, row_index=0, top_days=1, trim_days=1, upper_bound=None):
    row = study["results"][row_index]
    arguments = ["--snapshot", row["snapshot"], "--diagnostics", "--top-days", str(top_days),
                 "--trim-days", str(trim_days)]
    for name, period in study["periods"].items():
        arguments.extend([f"--{name}-start", period["start"], f"--{name}-end", period["end"]])
    if upper_bound is not None:
        arguments.extend(["--missing-daily-upper-bound", str(upper_bound)])
    output = io.StringIO()
    with redirect_stdout(output):
        code = analyze_main(arguments)
    if code != 0:
        raise AssertionError(output.getvalue())
    result = json.loads(output.getvalue())
    return save_json_artifact(result, root / f"analysis-{row['language']}.json"), result


def saved_chart(root, study, *, row_index=0):
    row = study["results"][row_index]
    arguments = ["--snapshot", row["snapshot"], "--output", str(root / f"chart-{row['language']}.png")]
    for name, period in study["periods"].items():
        arguments.extend([f"--{name}-start", period["start"], f"--{name}-end", period["end"]])
    output = io.StringIO()
    with redirect_stdout(output):
        code = chart_main(arguments)
    if code != 0:
        raise AssertionError(output.getvalue())
    result = json.loads(output.getvalue())
    artifact = save_json_artifact(result, root / f"chart-{row['language']}.json")
    return artifact, result


