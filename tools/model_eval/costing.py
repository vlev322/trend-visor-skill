import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from tools.pageviews.errors import PageviewsError


COST_FIELDS = {"input_per_million", "output_per_million", "currency", "tariff_date", "tariff_source"}


def _decimal_rate(value: object, name: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", value):
        raise PageviewsError("invalid_request", f"{name} must be a nonnegative decimal with at most 12 fractional digits.")
    try:
        rate = Decimal(value)
    except InvalidOperation as error:
        raise PageviewsError("invalid_request", f"Invalid {name}.") from error
    if not rate.is_finite() or rate > Decimal("1000000"):
        raise PageviewsError("invalid_request", f"{name} must not exceed 1000000 per million tokens.")
    return rate


def validate_tariff(value: dict | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != COST_FIELDS:
        raise PageviewsError("invalid_request", "Tariff requires input/output rates, ISO currency, date and source together.")
    input_rate = _decimal_rate(value["input_per_million"], "Input token rate")
    output_rate = _decimal_rate(value["output_per_million"], "Output token rate")
    currency = value["currency"]
    source = value["tariff_source"]
    try:
        tariff_day = date.fromisoformat(value["tariff_date"])
    except (TypeError, ValueError) as error:
        raise PageviewsError("invalid_request", "Tariff date must be a valid YYYY-MM-DD date.") from error
    if not isinstance(value["tariff_date"], str) or tariff_day.isoformat() != value["tariff_date"]:
        raise PageviewsError("invalid_request", "Tariff date must be a canonical YYYY-MM-DD date.")
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise PageviewsError("invalid_request", "Tariff currency must be a three-letter uppercase code.")
    if (not isinstance(source, str) or not source.strip() or len(source) > 1000
            or any(ord(char) < 32 for char in source)):
        raise PageviewsError("invalid_request", "Tariff source must be bounded, nonempty single-line text.")
    return {
        "input_per_million": str(input_rate), "output_per_million": str(output_rate),
        "currency": currency, "tariff_date": value["tariff_date"], "tariff_source": source,
    }


def summarize_cost(usage: dict, tariff: dict | None) -> dict:
    if tariff is None:
        return {
            "status": "unknown_not_configured", "amount": None, "currency": None,
            "tariff_date": None, "tariff_source": None,
            "reason": "No dated provider tariff was configured; unknown cost is not zero.",
        }
    clean = validate_tariff(tariff)
    if not usage.get("complete"):
        return {
            "status": "unknown_usage_incomplete", "amount": None,
            "currency": clean["currency"], "tariff_date": clean["tariff_date"],
            "tariff_source": clean["tariff_source"],
            "reason": "At least one model call has missing input/output usage; cost is not treated as zero.",
        }
    amount = (
        Decimal(usage["prompt_tokens"]) * Decimal(clean["input_per_million"])
        + Decimal(usage["completion_tokens"]) * Decimal(clean["output_per_million"])
    ) / Decimal(1_000_000)
    amount = amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    return {
        "status": "estimated_from_reported_usage", "amount": format(amount, "f"),
        "currency": clean["currency"], "tariff_date": clean["tariff_date"],
        "tariff_source": clean["tariff_source"],
        "basis": "provider-reported prompt/completion tokens multiplied by supplied rates per 1,000,000 tokens",
        "limitations": [
            "Estimate excludes provider-specific cached-token, reasoning, image, tool, tax or other charges unless included in supplied rates.",
            "Tariff source/date are caller-supplied and not independently fetched or verified.",
        ],
    }
