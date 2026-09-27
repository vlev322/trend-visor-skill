import hashlib
import hmac
import json
import math
import operator
import re
from decimal import Decimal, DecimalException
from fractions import Fraction
from pathlib import Path

from .artifacts import JsonArtifact
from .errors import PageviewsError
from .json_codec import strict_json_loads

CRITERIA_VERSION = 1
MAX_RULES_BYTES = 65536
METRICS = {
    "baseline_mean_daily_views": ("views_per_day", "Середні перегляди базового періоду"),
    "current_mean_daily_views": ("views_per_day", "Середні перегляди поточного періоду"),
    "change_percent": ("percent", "Описова відносна зміна"),
    "baseline_coverage_percent": ("percent", "Покриття базового періоду"),
    "current_coverage_percent": ("percent", "Покриття поточного періоду"),
    "change_without_top_days_percent": ("percent", "Зміна без найбільших днів"),
    "change_after_trim_percent": ("percent", "Зміна після обрізання країв"),
}
SCENARIO_PARAMETERS = {"change_without_top_days_percent": "top_days", "change_after_trim_percent": "trim_days"}
OPERATORS = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
SYMBOLS = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
DECISIONS = {
    "matches": "Кандидат для подальшої перевірки: відповідає погодженому правилу; це не рекомендація запускати продукт.",
    "does_not_match": "Не відповідає погодженому правилу; це не доказ відсутності аудиторії чи попиту.",
    "undetermined": "Недостатньо даних для рішення за погодженим правилом; спочатку перевірте наведені причини.",
}


def load_criteria_rules(reference: JsonArtifact, *, study_sha256: str, question: str) -> dict:
    if (not isinstance(reference, JsonArtifact) or not isinstance(reference.sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", reference.sha256) is None):
        raise PageviewsError("invalid_request", "Criteria rules require a file and its exact lowercase SHA256.")
    try:
        path = Path(reference.path).expanduser().resolve()
        with path.open("rb") as file:
            body = file.read(MAX_RULES_BYTES + 1)
        if len(body) > MAX_RULES_BYTES or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), reference.sha256):
            raise ValueError("Criteria rules size or checksum mismatch.")
        value = strict_json_loads(body)
        if not isinstance(value, dict) or set(value) != {"criteria_version", "study_sha256", "question", "match", "rules"}:
            raise ValueError("Unexpected criteria document fields.")
        if (type(value["criteria_version"]) is not int or value["criteria_version"] != CRITERIA_VERSION
                or value["study_sha256"] != study_sha256 or value["question"] != question
                or value["match"] not in ("all", "any")):
            raise ValueError("Criteria context, version or combination mode mismatch.")
        rules = value["rules"]
        if not isinstance(rules, list) or not 1 <= len(rules) <= 5:
            raise ValueError("Supply between one and five explicit rules.")
        # Strict decoding above rejects duplicates/non-finite values. This second, decimal
        # view is only for checking that threshold literals survived float decoding intact.
        exact_rules = json.loads(body, parse_float=Decimal)["rules"]
        seen = set()
        for rule, exact_rule in zip(rules, exact_rules, strict=True):
            if not isinstance(rule, dict) or set(rule) != {"id", "description", "metric", "operator", "threshold", "parameters"}:
                raise ValueError("Unexpected rule fields.")
            identifier, description, metric = rule["id"], rule["description"], rule["metric"]
            if not isinstance(identifier, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,39}", identifier) is None or identifier in seen:
                raise ValueError("Invalid or duplicate rule ID.")
            seen.add(identifier)
            if (not isinstance(description, str) or not description.strip() or len(description) > 200
                    or any(ord(char) < 32 for char in description)):
                raise ValueError("Expected bounded single-line rule description.")
            description.encode("utf-8")
            if not isinstance(metric, str) or metric not in METRICS or rule["operator"] not in OPERATORS:
                raise ValueError("Unsupported metric or operator; arbitrary expressions are not executed.")
            threshold = rule["threshold"]
            if type(threshold) not in (int, float) or not math.isfinite(threshold) or abs(threshold) > 10**15:
                raise ValueError("Rule thresholds must be finite numbers, not booleans, with magnitude at most 10^15.")
            if Decimal(str(threshold)) != exact_rule["threshold"]:
                raise ValueError("Threshold precision was lost in JSON decoding; use an exactly round-trippable decimal value.")
            parameters = rule["parameters"]
            parameter = SCENARIO_PARAMETERS.get(metric)
            if not isinstance(parameters, dict) or set(parameters) != ({parameter} if parameter else set()):
                raise ValueError("Supply exactly the scenario parameters required by the metric.")
            if parameter and (type(parameters[parameter]) is not int or not 1 <= parameters[parameter] <= 1000000):
                raise ValueError("Scenario day counts must be integers between 1 and 1000000.")
        return {"document": value, "path": str(path), "sha256": reference.sha256}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError, DecimalException) as error:
        raise PageviewsError("criteria_rules_error", "Criteria rules are missing, changed, unsupported or inconsistent with this question/study.",
                             details={"exception_type": type(error).__name__}) from error


def _change(analysis: dict) -> tuple[Fraction | None, str | None]:
    if analysis["comparison"]["status"] != "computed":
        return None, analysis["comparison"]["reason"]
    baseline, current = analysis["baseline"], analysis["current"]
    before = Fraction(baseline["sum_observed_views"], baseline["coverage"]["observed_days"])
    after = Fraction(current["sum_observed_views"], current["coverage"]["observed_days"])
    return 100 * (after / before - 1), None


def _scenario_value(row: dict, rule: dict) -> tuple[Fraction | None, str | None]:
    diagnostics = row.get("diagnostics")
    if diagnostics is None:
        return None, "diagnostics_not_supplied"
    parameter = SCENARIO_PARAMETERS[rule["metric"]]
    if diagnostics["parameters"][parameter] != rule["parameters"][parameter]:
        return None, "diagnostic_parameters_mismatch"
    if parameter == "trim_days":
        edges = diagnostics["window_edges"]
        if edges["status"] != "computed":
            return None, edges["reason"]
        return _change(edges["scenario"])
    largest = diagnostics["largest_days"]
    if largest["comparison_after_exclusion"]["status"] != "computed":
        return None, largest["comparison_after_exclusion"]["reason"]
    before, after = (Fraction(largest[name]["sum_observed_views_after_exclusion"], largest[name]["remaining_observed_days"])
                     for name in ("baseline", "current"))
    return 100 * (after / before - 1), None


def _value(row: dict, rule: dict) -> tuple[Fraction | None, str | None]:
    if row["status"] != "analyzed":
        return None, row["reason"]
    metric = rule["metric"]
    if metric in SCENARIO_PARAMETERS:
        return _scenario_value(row, rule)
    analysis = row["analysis"]
    if metric == "change_percent":
        return _change(analysis)
    name = "baseline" if metric.startswith("baseline_") else "current"
    period = analysis[name]
    coverage = period["coverage"]
    if metric.endswith("coverage_percent"):
        return Fraction(100 * coverage["observed_days"], coverage["expected_days"]), None
    if period["status"] != "complete":
        return None, f"incomplete_{name}_coverage"
    return Fraction(period["sum_observed_views"], coverage["observed_days"]), None


def evaluate_criteria(row: dict, policy: dict, *, attachments=()) -> dict:
    checks = []
    for rule in policy["document"]["rules"]:
        value, reason = _value(row, rule)
        status = "undetermined" if value is None else (
            "matches" if OPERATORS[rule["operator"]](value, Fraction(str(rule["threshold"]))) else "does_not_match"
        )
        checks.append({
            "rule_id": rule["id"], "status": status, "value": round(float(value), 6) if value is not None else None,
            "exact_value": {"numerator": value.numerator, "denominator": value.denominator} if value is not None else None,
            "unit": METRICS[rule["metric"]][0], "reason": reason, "evidence_id": row["evidence_id"],
        })
        if rule["metric"] in SCENARIO_PARAMETERS:
            checks[-1]["analysis_sha256"] = next((entry["sha256"] for entry in attachments
                                                   if entry["operation"] == "analyze" and entry["language"] == row["language"]), None)
    states = {check["status"] for check in checks}
    if policy["document"]["match"] == "all":
        status = "does_not_match" if "does_not_match" in states else ("undetermined" if "undetermined" in states else "matches")
    else:
        status = "matches" if "matches" in states else ("undetermined" if "undetermined" in states else "does_not_match")
    return {"status": status, "checks": checks, "message_uk": DECISIONS[status]}


def criteria_summary(policy: dict, rows: list[dict]) -> dict:
    return {
        "status": "evaluated", "kind": "followup_screening_not_market_ranking",
        "rules_sha256": policy["sha256"], "match": policy["document"]["match"], "rules": policy["document"]["rules"],
        "counts": {status: sum(row["criteria_evaluation"]["status"] == status for row in rows) for status in DECISIONS},
        "order": "requested_languages", "confirmation": "caller_supplied_file_checksum",
        "message_uk": "Кандидатів визначено лише за явно заданим правилом на перевірених даних; порядок мов збережено, оцінки ринків і статистичного рейтингу немає.",
    }


def criteria_text(row: dict, summary: dict) -> str:
    evaluation = row["criteria_evaluation"]
    sentences = [evaluation["message_uk"]]
    rules = {rule["id"]: rule for rule in summary["rules"]}
    states = {"matches": "так", "does_not_match": "ні", "undetermined": "невідомо"}
    for check in evaluation["checks"]:
        rule = rules[check["rule_id"]]
        exact = check["exact_value"]
        measured = (str(exact["numerator"]) if exact["denominator"] == 1 else f"{exact['numerator']}/{exact['denominator']}") if exact else "невідомо"
        unit = "%" if check["unit"] == "percent" else " переглядів/день"
        if exact is not None:
            measured += unit
        suffix = f"; причина: {check['reason']}" if check["reason"] else ""
        settings = ", ".join(f"{name}={value}" for name, value in rule["parameters"].items())
        if settings:
            suffix += f"; параметри: {settings}"
        sentences.append(f"{rule['id']} — {METRICS[rule['metric']][1]} {SYMBOLS[rule['operator']]} {rule['threshold']}{unit}: "
                 f"{states[check['status']]}; значення {measured}{suffix}.")
    return " ".join(sentences)