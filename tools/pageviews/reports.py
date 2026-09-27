import hashlib
import html
import json
import re
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import quote

from . import SCHEMA_VERSION
from .analysis import Period, analyze_series_zero_filled, validate_period_order
from .calendar_analysis import compare_calendar_months
from .diagnostics import DEFAULT_TOP_DAYS, DEFAULT_TRIM_DAYS, run_diagnostics
from .errors import PageviewsError
from .models import parse_date
from .narrative import diagnostic_text, period_direction, period_text
from .storage import read_snapshot
from .studies import DERIVED_STUDY_VERSION, STUDY_VERSION
from .validation import fill_missing_as_zero, validate_response

REPORT_VERSION = 5
MAX_EVIDENCE_BYTES = 24000
LIMITATIONS = [
    "Перегляди — це події, не унікальні люди, не весь інтерес до теми й не доказ попиту на продукт.",
    "Мовні розділи не є країнами або ринками; статті одного Wikidata item можуть відрізнятися за охопленням.",
    "Порівняння описове: без сезонного коригування, прогнозу або статистичного рейтингу мов.",
    "Wikimedia не повертає дні з нульовими переглядами; такі дні тут пораховано як 0 (див. assumed_zero_days).",
    "Перегляди стосуються підтвердженої назви; переходи через інші назви й історичні перейменування не об'єднані.",
    "Класифікація Wikimedia user не гарантує відсутності всіх ботів.",
    "Зміна трафіку однієї статті може відображати зміну всього мовного розділу, а не лише теми.",
]


def _text(value: object, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise PageviewsError("invalid_request", f"{name} must be nonempty single-line text of at most {limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PageviewsError("invalid_request", f"{name} must be UTF-8 encodable.") from None
    return value


def _markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{\[\]}()#+.!|>~-])", r"\\\1", html.escape(value, quote=False))


def _next_check(row: dict) -> dict:
    comparison = row.get("analysis", {}).get("comparison", {})
    if row["status"] == "not_collected":
        code, reason = "review_mapping", row["reason"]
        action = "Уточнити відповідність статті; відсутність підтвердженого збігу не означає відсутності аудиторії."
    elif row["status"] == "collection_failed":
        code, reason = "review_collection", row["reason"]
        action = "Перевірити помилку збору; новий запит виконувати лише явно, з урахуванням лімітів джерела."
    elif comparison["reason"] == "incomplete_coverage":
        code, reason = "review_coverage", "incomplete_coverage"
        action = "Перевірити пропущені спостереження; повна відносна зміна недоступна, нульове заповнення не обґрунтоване."
    elif comparison["reason"] == "zero_baseline":
        code, reason = "compare_levels", "zero_baseline"
        action = "Розглянути абсолютні рівні: за нульової бази відносний відсоток не визначений."
    else:
        code, reason = "review_page_history", period_direction(row["analysis"])
        action = "Зіставити зафіксовану зміну з історією назви й змісту цієї статті, перш ніж пояснювати причини."
    return {"code": code, "reason": reason, "text_uk": action}


def _reliability_text(row: dict) -> str:
    if row["status"] != "analyzed":
        return (f"{row['language']}: цілісність/покриття — недоступні ({row['reason']}); "
                "чутливість і календарну узгодженість не оцінено.")
    analysis = row["analysis"]
    periods = []
    for name in ("baseline", "current"):
        item = analysis[name]
        coverage = item["coverage"]
        periods.append(
            f"{name} {item['status']} ({coverage['observed_days']}/{coverage['expected_days']}, "
            f"пропусків {coverage['missing_days']})"
        )
    calendar = row.get("calendar")
    if calendar is None:
        calendar_text = "не обчислена"
    else:
        summary = calendar["summary"]
        calendar_text = f"{summary['current_months']} пар, обчислено {summary['computed_pairs']}, виключено {summary['excluded_pairs']}"
    return (
        f"{row['language']}: покриття — {'; '.join(periods)}; "
        f"чутливість — окремі описові сценарії (найбільші дні, обрізані краї вікна); "
        f"календарна узгодженість — {calendar_text}; статистичний висновок не виконується."
    )


def _analyze_row(row: dict, baseline: Period, current: Period, *, chart_dir: Path | None) -> dict:
    """Recompute descriptive analysis, calendar consistency, sensitivity and an
    optional chart directly from the pinned snapshot; never a new HTTP request."""
    evidence: dict[str, object] = {
        "language": row["language"], "project": row["project"], "article": row["article"],
        "status": row["status"], "reason": row["reason"],
    }
    if row["status"] != "analyzed":
        return evidence
    try:
        snapshot = read_snapshot(Path(row["snapshot"]))
        series = validate_response(snapshot.response.body, snapshot.request)
        filled = fill_missing_as_zero(series)
        analysis = analyze_series_zero_filled(series, baseline, current)
        calendar = compare_calendar_months(filled, baseline, current)
        diagnostics = run_diagnostics(
            filled, baseline, current, top_days=DEFAULT_TOP_DAYS, trim_days=DEFAULT_TRIM_DAYS,
        )
    except PageviewsError as error:
        raise PageviewsError(
            "report_source_error", "A pinned snapshot failed report verification.",
            details={"language": row["language"], "source_code": error.code},
        ) from error
    # Per-pair month rows are unused by the report/PDF text; keep only the compact summary.
    calendar_summary = {key: calendar[key] for key in ("method", "status", "summary")}
    evidence.update(analysis=analysis, calendar=calendar_summary, diagnostics=diagnostics,
                    source=snapshot.response.source_metadata())
    if chart_dir is not None:
        if find_spec("matplotlib") is None:
            raise PageviewsError("missing_dependency", "Install the charts extra before requesting --chart-dir.")
        from .charts import ChartSource, prepare_chart_data, render_png, save_png

        periods = prepare_chart_data(series, baseline, current)
        source = ChartSource(
            article=row["article"], project=row["project"], url=snapshot.response.url,
            fetched_at=snapshot.response.fetched_at, response_sha256=snapshot.response.sha256,
        )
        png = render_png(periods, source)
        path = save_png(png, chart_dir / f"{row['language']}.png")
        evidence["chart"] = {"path": str(path), "sha256": hashlib.sha256(png).hexdigest()}
    return evidence


def _render(report: dict) -> str:
    baseline, current = report["periods"]["baseline"], report["periods"]["current"]
    lines = ["# Звіт про перегляди Wikipedia", "", "## Питання та межі", "",
             _markdown_text(report["question"]), "",
             f"Базовий період: {baseline['start']}–{baseline['end']}; поточний: {current['start']}–{current['end']}.",
             f"Опорна дата as-of (UTC): {report['as_of']}; буфер завершених днів: {report['excluded_recent_days']}.",
             "Критерії користувача: " + ("; ".join(_markdown_text(item) for item in report["criteria"]) or "не задані"),
             "", "## Що показують дані", ""]
    for row in report["evidence"]:
        lines.extend([_markdown_text(period_text(row)), ""])
        if "diagnostics" in row:
            lines.extend([_markdown_text(diagnostic_text(row["diagnostics"])), ""])
        if "chart" in row:
            lines.extend([f"{row['language']}: [Відкрити перевірений PNG]({Path(row['chart']['path']).name})", ""])
    lines.extend(["## Що перевірити далі й чому", ""])
    lines.extend([report["prioritization"]["message_uk"], ""])
    for row in report["evidence"]:
        check = row["next_check"]
        lines.append(f"- {_markdown_text(row['language'])} ({check['reason']}): {check['text_uk']}")
    lines.extend(["", "Попит на продукт перевіряти окремо з його користувачами; Wikipedia лише допомагає сформувати гіпотези.",
                  "", "## Надійність і обмеження", "",
                  "Наведені виміри не зводяться до загального бала довіри:", ""])
    lines.extend(_reliability_text(row) for row in report["evidence"])
    lines.append("")
    lines.extend("- " + item for item in report["limitations_uk"])
    if report.get("summary_uk"):
        lines.extend(["", "## Висновок агента", "",
                      "Наступний текст сформував агент за результатами вище; це не перевірений код-факт:", "",
                      _markdown_text(report["summary_uk"])])
    lines.extend(["", "## Джерела", ""])
    for row in report["evidence"]:
        source = row.get("source")
        if source is not None:
            url = quote(source["url"], safe=":/?&=%#-._~")
            lines.append(f"- {row['language']}: [Wikimedia API]({url}); отримано {source['fetched_at_utc']}; raw SHA256: {source['response_sha256']}")
    lines.extend(["", "Проаналізовані статті повторно перераховано з точних збережених snapshot-ів; мережевих запитів не виконано."])
    return "\n".join(lines) + "\n"


def build_report(
    study_path: Path, *, question: str, criteria=(), summary: str | None = None, chart_dir: Path | None = None,
) -> dict:
    question = _text(question, 1000, "question")
    if not isinstance(criteria, (tuple, list)) or len(criteria) > 5:
        raise PageviewsError("invalid_request", "Supply at most five textual criteria.")
    criteria = [_text(item, 200, "criterion") for item in criteria]
    if summary is not None:
        summary = _text(summary, 4000, "summary")
    try:
        study = json.loads(study_path.expanduser().resolve().read_text(encoding="utf-8"))
        if (
            type(study.get("schema_version")) is not int or study["schema_version"] != SCHEMA_VERSION
            or study.get("study_version") not in (STUDY_VERSION, DERIVED_STUDY_VERSION)
            or study.get("operation") != "study"
        ):
            raise ValueError("Unsupported study format.")
        if not isinstance(study.get("results"), list) or not study["results"]:
            raise ValueError("Study has no requested-language results.")
        baseline = Period(
            parse_date(study["periods"]["baseline"]["start"], "baseline_start"),
            parse_date(study["periods"]["baseline"]["end"], "baseline_end"),
        )
        current = Period(
            parse_date(study["periods"]["current"]["start"], "current_start"),
            parse_date(study["periods"]["current"]["end"], "current_end"),
        )
        validate_period_order(baseline, current)
    except PageviewsError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise PageviewsError(
            "report_source_error", "The referenced study cannot be read as a valid study JSON.",
            details={"exception_type": type(error).__name__},
        ) from error

    evidence = [_analyze_row(row, baseline, current, chart_dir=chart_dir) for row in study["results"]]
    for row in evidence:
        row["evidence_id"] = row["language"]
        row["next_check"] = _next_check(row)

    if not criteria and len(evidence) == 1:
        prioritization = {
            "status": "not_applicable_single_language",
            "message_uk": "Це дослідження охоплює лише одну мовну версію; міжмовна пріоритизація не застосовується.",
        }
    else:
        prioritization = {
            "status": "criteria_require_review" if criteria else "needs_criteria",
            "message_uk": (
                "Критерії записані дослівно; агент застосовує їх до наведених нижче фактів, а не до автоматичної формули."
                if criteria else "Критерії відбору аудиторій не задані: агент застосовує власне судження до наведених фактів."
            ),
        }

    identity = {"report_version": REPORT_VERSION, "study_path": str(study_path), "question": question, "criteria": criteria}
    report_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    report = {
        **identity, "report_id": report_id, "operation": "report", "status": "completed",
        "periods": study["periods"], "as_of": study["as_of"], "excluded_recent_days": study["excluded_recent_days"],
        "evidence": evidence, "prioritization": prioritization, "limitations_uk": list(LIMITATIONS),
        "summary_uk": summary, "narrative_review_required": True,
    }
    report["markdown"] = _render(report)
    return report


def evidence_json(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise PageviewsError("evidence_too_large", f"Evidence response exceeds {MAX_EVIDENCE_BYTES} UTF-8 bytes; reduce the page size or input text. No fields were dropped.")
    return text


def evidence_page(report: dict, *, offset: int = 0, limit: int = 3) -> dict:
    rows = report["evidence"]
    if type(offset) is not int or not 0 <= offset < len(rows) or type(limit) is not int or not 1 <= limit <= 10:
        raise PageviewsError("invalid_request", "Choose an existing evidence offset and a limit between 1 and 10.")
    end = min(offset + limit, len(rows))
    result = {
        key: report[key] for key in ("report_version", "report_id", "operation", "status", "question",
                                    "criteria", "periods", "as_of", "prioritization",
                                    "narrative_review_required")
    }
    result.update(total_rows=len(rows), offset=offset, next_offset=end if end < len(rows) else None,
                  evidence=rows[offset:end], limitations_uk=report["limitations_uk"], summary_uk=report["summary_uk"])
    evidence_json(result)
    return deepcopy(result)
