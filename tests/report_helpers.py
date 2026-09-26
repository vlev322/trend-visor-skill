from datetime import date, timedelta

from tests.helpers import encode_items, make_item, make_request
from tests.study_helpers import TITLES, resolution_result
from tools.pageviews.analysis import Period
from tools.pageviews.artifacts import save_json_artifact
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
