import hashlib
import html
import json
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from .errors import PageviewsError
from .narrative import diagnostic_text, period_direction, period_text
from .report_attachments import load_report_attachments
from .report_criteria import criteria_summary, criteria_text, evaluate_criteria, load_criteria_rules
from .report_sources import load_report_source

REPORT_VERSION = 4
MAX_EVIDENCE_BYTES = 24000
LIMITATIONS = [
    "Перегляди — це події, не унікальні люди, не весь інтерес до теми й не доказ попиту на продукт.",
    "Мовні розділи не є країнами або ринками; статті одного Wikidata item можуть відрізнятися за охопленням.",
    "Порівняння описове: без сезонного коригування, прогнозу або статистичного рейтингу мов.",
    "Єдиного показника довіри чи довірчого інтервалу для описової зміни немає: покриття, чутливість і календарна узгодженість — різні перевірки, а повне покриття не є статистичною впевненістю.",
    "Пропуски залишаються невідомими, а не нулями. Повнота календаря не доводить безпомилковість вимірювання.",
    "Перегляди стосуються підтвердженої назви; переходи через інші назви й історичні перейменування не об'єднані.",
    "Класифікація Wikimedia user не гарантує відсутності всіх ботів. Checksum перевіряє цілісність, не автентичність джерела.",
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
    return {"code": code, "reason": reason, "text_uk": action, "evidence_id": row["evidence_id"]}


def _reliability_text(row: dict) -> str:
    if row["status"] != "analyzed":
        return (f"{row['language']}: цілісність/покриття — недоступні ({row['reason']}); "
                "чутливість і календарну узгодженість не оцінено; статистичний висновок не надається.")
    analysis = row["analysis"]
    periods = []
    for name in ("baseline", "current"):
        item = analysis[name]
        coverage = item["coverage"]
        periods.append(
            f"{name} {item['status']} ({coverage['observed_days']}/{coverage['expected_days']}, "
            f"пропусків {coverage['missing_days']})"
        )
    if "diagnostics" in row:
        sensitivity = "перевірена за pinned diagnostics; сценарії та unavailable причини наведено вище"
    else:
        sensitivity = "не перевірена (diagnostics_not_supplied)"
    calendar = analysis.get("methodology", {}).get("calendar_comparison")
    if calendar is None:
        calendar_text = "не записана в study"
    else:
        summary = calendar["summary"]
        calendar_text = (f"{summary['current_months']} пар, "
                 f"обчислено {summary['computed_pairs']}, виключено {summary['excluded_pairs']}")
    statistical = (
        "збережена модель не переоцінена й не наведена"
        if row.get("recorded_model") == "not_revalidated_not_reported"
        else "не виконувався в цьому report"
    )
    return (
        f"{row['language']}: цілісність — описові числа повторно обчислено з pinned snapshot; "
        f"покриття — {'; '.join(periods)}; чутливість — {sensitivity}; "
        f"календарна узгодженість — {calendar_text}; статистичний висновок — {statistical}."
    )


def _render(report: dict) -> str:
    baseline, current = report["periods"]["baseline"], report["periods"]["current"]
    descriptions = report["criteria"] or [rule["description"] for rule in report["prioritization"].get("rules", [])]
    lines = ["# Звіт про перегляди Wikipedia", "", "## Питання та межі", "",
             _markdown_text(report["question"]), "",
             f"Базовий період: {baseline['start']}–{baseline['end']}; поточний: {current['start']}–{current['end']}.",
             f"Опорна дата as-of (UTC): {report['as_of']}; буфер завершених днів: {report['excluded_recent_days']}.",
             "Критерії користувача: " + ("; ".join(_markdown_text(item) for item in descriptions) or "не задані"),
             "", "## Що показують дані", ""]
    for row in report["evidence"]:
        lines.extend([_markdown_text(period_text(row, row)), ""])
        if "diagnostics" in row:
            lines.extend([_markdown_text(diagnostic_text(row["diagnostics"])), ""])
        if "chart" in row:
            lines.extend([f"{row['language']}: [Відкрити перевірений PNG]({Path(row['chart']['path']).as_uri()})", ""])
    lines.extend(["## Що перевірити далі й чому", ""])
    if report["prioritization"]["status"] == "evaluated":
        mode = "усі умови" if report["prioritization"]["match"] == "all" else "хоча б одна умова"
        lines.extend([f"Правило відбору: {mode}. Невідомі значення залишаються невідомими.", ""])
    for row in report["evidence"]:
        if "criteria_evaluation" in row:
            lines.extend([f"{_markdown_text(row['language'])}: {_markdown_text(criteria_text(row, report['prioritization']))}", ""])
        check = row["next_check"]
        lines.append(f"- {_markdown_text(row['language'])} ({check['reason']}): {check['text_uk']}")
        if "criteria_evaluation" in row:
            lines.append("")
    lines.extend(["", report["prioritization"]["message_uk"], "",
                  "Попит на продукт перевіряти окремо з його користувачами; Wikipedia лише допомагає сформувати гіпотези.",
                  "", "## Надійність і обмеження", "",
                  "Наведені виміри не зводяться до загального бала довіри:", ""])
    lines.extend(_reliability_text(row) for row in report["evidence"])
    lines.append("")
    lines.extend("- " + item for item in report["limitations_uk"])
    lines.extend(["", "## Джерела та перевірки", "",
                  f"Дослідження SHA256: {report['study_sha256']}",
                  f"Підтверджена відповідність SHA256: {report['audit']['resolution']['sha256']}", ""])
    for row in report["evidence"]:
        source = row.get("source")
        if source is not None:
            url = quote(source["url"], safe=":/?&=%#-._~")
            lines.append(f"- {row['language']}: [Wikimedia API]({url}); отримано {source['fetched_at_utc']}; raw SHA256: {source['response_sha256']}")
    lines.extend(["", "За наявності проаналізованих статей їхні описові числа повторно обчислено з точних збережених snapshot-ів; мережевих запитів не виконано."])
    return "\n".join(lines) + "\n"


def build_report(path: Path, checksum: str, *, question: str, criteria=(),
                 analysis_artifacts=(), chart_artifacts=(), criteria_rules=None) -> dict:
    question = _text(question, 1000, "question")
    if not isinstance(criteria, (tuple, list)) or len(criteria) > 5:
        raise PageviewsError("invalid_request", "Supply at most five textual criteria.")
    criteria = [_text(item, 200, "criterion") for item in criteria]
    if criteria and criteria_rules is not None:
        raise PageviewsError("invalid_request", "Use reviewed criteria rules or unstructured criteria, not both; no criterion may be silently ignored.")
    policy = load_criteria_rules(criteria_rules, study_sha256=checksum, question=question) if criteria_rules is not None else None
    source = load_report_source(path, checksum)
    attachments, audit = load_report_attachments(source, analysis_artifacts=analysis_artifacts, chart_artifacts=chart_artifacts)
    source["attachments"] = audit
    identity = {"report_version": REPORT_VERSION, "study_sha256": checksum, "question": question, "criteria": criteria,
                "criteria_rules_sha256": policy["sha256"] if policy is not None else None,
                "attachment_sources": [{key: item[key] for key in ("operation", "language", "sha256")} for item in audit]}
    report_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    evidence = source.pop("rows")
    for row in evidence:
        row.update(attachments.get(row["language"], {}))
        row["additional_evidence"] = {"diagnostics": "verified" if "diagnostics" in row else "not_supplied",
                                      "chart": "verified" if "chart" in row else "not_supplied"}
        row["evidence_id"] = f"{checksum}:{row['language']}"
        row["next_check"] = _next_check(row)
        if policy is not None:
            row["criteria_evaluation"] = evaluate_criteria(row, policy, attachments=audit)
    limits = list(LIMITATIONS)
    if analysis_artifacts:
        limits.append("Перевірки чутливості — окремі описові сценарії, не довірчі інтервали й не послідовне очищення даних. "
                      "Вони не змінюють вихідну оцінку й не доводять стійкість або причину зміни; параметри взято із збереженого аналізу, не оптимізовано.")
    if chart_artifacts:
        limits.append("Для PNG перевірено цілісність, вбудовані метадані та відповідність джерелу/періодам; "
                      "це не замінює візуальний огляд зображення. Локальне посилання потребує доступу до самого PNG.")
    if any(row.get("recorded_model") == "not_revalidated_not_reported" for row in evidence):
        limits.append("Збережене дослідження містить статистичну модель. Цей звіт не переоцінює її та не відтворює її нахил/інтервал; висновки тут лише описові.")
    prioritization = {
        "status": "criteria_require_review" if criteria else "needs_criteria",
        "message_uk": (
            "Критерії записані дослівно, але ще не перетворені на погоджене правило відбору. Пріоритет мов автоматично не визначено."
            if criteria else "Пріоритет мов не визначено: спочатку погодьте критерії відбору аудиторій для подальшої перевірки."
        ),
    }
    if not criteria and len(evidence) == 1:
        prioritization = {
            "status": "not_applicable_single_language",
            "message_uk": "Це дослідження охоплює лише одну мовну версію; міжмовна пріоритизація не застосовується.",
        }
    if policy is not None:
        prioritization = criteria_summary(policy, evidence)
        source["criteria_rules"] = {"path": policy["path"], "sha256": policy["sha256"]}
        limits.append("Виконання погоджених порогів лише визначає кандидатів для подальшої перевірки. "
                      "Пороги не є статистичними межами впевненості; checksum фіксує передане правило, не доводить особу чи факт людського погодження.")
    report = {
        **identity, "report_id": report_id, "operation": "report", "status": "completed",
        "scope": "verified_descriptive_study", "periods": source["periods"], "as_of": source["as_of"],
        "excluded_recent_days": source["excluded_recent_days"], "evidence": evidence,
        "prioritization": prioritization, "limitations_uk": limits, "audit": source,
        "narrative_review_required": True,
        "verification": {"descriptive": "recomputed_and_matched", "calendar": "recomputed_when_recorded",
                         "diagnostics": "recomputed_and_matched" if analysis_artifacts else "not_supplied",
                         "charts": "checksum_metadata_and_source_matched" if chart_artifacts else "not_supplied",
                         "statistical_fit": "not_revalidated_not_reported", "network_requests": 0},
    }
    report["markdown"] = _render(report)
    return report


def evidence_json(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise PageviewsError("evidence_too_large", f"Evidence response exceeds {MAX_EVIDENCE_BYTES} UTF-8 bytes; reduce the page size or input text. No fields were dropped.")
    return text


def evidence_page(report: dict, *, offset: int = 0, limit: int = 1) -> dict:
    rows = report["evidence"]
    if type(offset) is not int or not 0 <= offset < len(rows) or type(limit) is not int or not 1 <= limit <= 3:
        raise PageviewsError("invalid_request", "Choose an existing evidence offset and a limit between 1 and 3.")
    end = min(offset + limit, len(rows))
    result = {
        key: report[key] for key in ("report_version", "report_id", "operation", "status", "scope", "question",
                                    "criteria", "study_sha256", "periods", "as_of", "prioritization", "verification",
                                    "narrative_review_required")
    }
    result.update(total_rows=len(rows), offset=offset, next_offset=end if end < len(rows) else None,
                  evidence=rows[offset:end], limitations_uk=report["limitations_uk"],
                  detail_policy="Paginated language summaries; full data remain in the pinned study and snapshots.")
    if "research_state" in report:
        result["research_state"] = report["research_state"]
    evidence_json(result)
    return deepcopy(result)
