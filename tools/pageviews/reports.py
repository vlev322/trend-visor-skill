import hashlib
import html
import json
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from .errors import PageviewsError
from .narrative import period_direction, period_text
from .report_sources import load_report_source

REPORT_VERSION = 1
MAX_EVIDENCE_BYTES = 12000
LIMITATIONS = [
    "Перегляди — це події, не унікальні люди, не весь інтерес до теми й не доказ попиту на продукт.",
    "Мовні розділи не є країнами або ринками; статті одного Wikidata item можуть відрізнятися за охопленням.",
    "Порівняння описове: без сезонного коригування, прогнозу або статистичного рейтингу мов.",
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


def _render(report: dict) -> str:
    baseline, current = report["periods"]["baseline"], report["periods"]["current"]
    lines = ["# Звіт про перегляди Wikipedia", "", "## Питання та межі", "",
             _markdown_text(report["question"]), "",
             f"Базовий період: {baseline['start']}–{baseline['end']}; поточний: {current['start']}–{current['end']}.",
             f"Опорна дата as-of (UTC): {report['as_of']}; буфер завершених днів: {report['excluded_recent_days']}.",
             "Критерії користувача: " + ("; ".join(_markdown_text(item) for item in report["criteria"]) or "не задані"),
             "", "## Що показують дані", ""]
    for row in report["evidence"]:
        lines.extend([_markdown_text(period_text(row, {})), ""])
    lines.extend(["## Що перевірити далі й чому", ""])
    for row in report["evidence"]:
        check = row["next_check"]
        lines.append(f"- {_markdown_text(row['language'])} ({check['reason']}): {check['text_uk']}")
    lines.extend(["", report["prioritization"]["message_uk"], "",
                  "Попит на продукт перевіряти окремо з його користувачами; Wikipedia лише допомагає сформувати гіпотези.",
                  "", "## Надійність і обмеження", ""])
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


def build_report(path: Path, checksum: str, *, question: str, criteria=()) -> dict:
    question = _text(question, 1000, "question")
    if not isinstance(criteria, (tuple, list)) or len(criteria) > 5:
        raise PageviewsError("invalid_request", "Supply at most five textual criteria.")
    criteria = [_text(item, 200, "criterion") for item in criteria]
    source = load_report_source(path, checksum)
    identity = {"report_version": REPORT_VERSION, "study_sha256": checksum, "question": question, "criteria": criteria}
    report_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    evidence = source.pop("rows")
    for row in evidence:
        row["evidence_id"] = f"{checksum}:{row['language']}"
        row["next_check"] = _next_check(row)
    limits = list(LIMITATIONS)
    if any(row.get("recorded_model") == "not_revalidated_not_reported" for row in evidence):
        limits.append("Збережене дослідження містить статистичну модель. Цей звіт не переоцінює її та не відтворює її нахил/інтервал; висновки тут лише описові.")
    prioritization = {
        "status": "criteria_require_review" if criteria else "needs_criteria",
        "message_uk": (
            "Критерії записані дослівно, але ще не перетворені на погоджене правило відбору. Пріоритет мов автоматично не визначено."
            if criteria else "Пріоритет мов не визначено: спочатку погодьте критерії відбору аудиторій для подальшої перевірки."
        ),
    }
    report = {
        **identity, "report_id": report_id, "operation": "report", "status": "completed",
        "scope": "verified_descriptive_study", "periods": source["periods"], "as_of": source["as_of"],
        "excluded_recent_days": source["excluded_recent_days"], "evidence": evidence,
        "prioritization": prioritization, "limitations_uk": limits, "audit": source,
        "narrative_review_required": True,
        "verification": {"descriptive": "recomputed_and_matched", "calendar": "recomputed_when_recorded",
                         "statistical_fit": "not_revalidated_not_reported", "network_requests": 0},
    }
    report["markdown"] = _render(report)
    return report


def evidence_json(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise PageviewsError("evidence_too_large", "Evidence response exceeds 12000 UTF-8 bytes; reduce the page size or input text. No fields were dropped.")
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
    evidence_json(result)
    return deepcopy(result)
