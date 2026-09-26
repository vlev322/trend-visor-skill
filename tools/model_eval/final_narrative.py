from fractions import Fraction

NARRATIVE_VERSION = 1
RENDERED_FIELDS = ("summary_uk", "limitations_uk", "next_steps_uk")
DIRECTIONS = ("increase", "decrease", "unchanged", "not_computed")
NEXT_CHECKS = {
    "review_page_history": "Перевірити історію назви, змісту й охоплення тієї самої статті перед поясненням причин зміни.",
    "check_measurement_coverage": "Перевірити повноту та правила вимірювання переглядів; пропуски не замінювати нулями без обґрунтування.",
    "review_model_assumptions": "Окремо перевірити припущення моделі та її залишки; не підбирати метод або параметри за бажаним висновком.",
    "compare_prespecified_windows": "За потреби перевірити інші наперед обрані історичні періоди тієї самої статті, зберігаючи вихідний результат.",
    "validate_interest_with_users": "Окремо перевірити попит на продукт серед користувачів застосунку: інтерв’ю, опитування або тест пропозиції.",
}


def _direction(analysis: dict) -> str:
    if analysis.get("comparison", {}).get("status") != "computed":
        return "not_computed"
    baseline, current = analysis["baseline"], analysis["current"]
    before = Fraction(baseline["sum_observed_views"], baseline["coverage"]["observed_days"])
    after = Fraction(current["sum_observed_views"], current["coverage"]["observed_days"])
    return "increase" if after > before else ("decrease" if after < before else "unchanged")


def interpretations(results: dict) -> list[dict]:
    return [{
        "language": row["language"], "period_change_direction": _direction(row.get("analysis", {})),
        "trend_interval_available": row.get("analysis", {}).get("methodology", {}).get(
            "trend_model", {},
        ).get("confidence_interval", {}).get("status") == "computed",
    } for row in results["study_fresh"]["results"]]


def _number(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "невідомо"
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return text.replace(",", " ").replace(".", ",").replace("-", "−")


def _period_text(row: dict, detailed: dict) -> str:
    language = row["language"]
    if row["status"] != "analyzed":
        return f"{language}: аналіз статті недоступний ({row['reason']}); це не доказ відсутності інтересу."
    analysis = row["analysis"]
    baseline, current = analysis["baseline"], analysis["current"]
    before = f"{baseline['window']['start']}–{baseline['window']['end']}"
    after = f"{current['window']['start']}–{current['window']['end']}"
    title = row.get("article") or language
    sentences = [f"Стаття «{title}» ({row.get('project', language)}), періоди {before} та {after}."]
    direction = _direction(analysis)
    means = (_number(baseline["mean_daily_views_observed"]), _number(current["mean_daily_views_observed"]))
    if direction == "not_computed":
        reason = analysis["comparison"]["reason"]
        sentences.append(f"Середні за спостережені дні — {means[0]} та {means[1]} на день; відносну зміну не обчислено ({reason}).")
    else:
        verb = {"increase": "зросли", "decrease": "знизилися", "unchanged": "не змінилися"}[direction]
        change = _number(analysis["comparison"]["change_percent"], signed=True)
        sentences.append(f"Середні перегляди {verb} з {means[0]} до {means[1]} на день; описова зміна — {change}%.")
    b, c = baseline["coverage"], current["coverage"]
    sentences.append(
        f"Спостережено {b['observed_days']}/{b['expected_days']} і {c['observed_days']}/{c['expected_days']} днів; "
        f"пропущено загалом {b['missing_days'] + c['missing_days']}."
    )
    months = analysis.get("methodology", {}).get("calendar_comparison", {}).get("summary", {})
    if months.get("computed_pairs"):
        sentences.append(
            f"Повних порівнянних місячних пар: {months['computed_pairs']}; "
            f"зі зниженням — {months['lower_pairs']}, зі зростанням — {months['higher_pairs']}, "
            f"без зміни — {months['equal_pairs']}; виключено — {months['excluded_pairs']}."
        )
    diagnostics = detailed.get("diagnostics", {})
    parameters = diagnostics.get("parameters", {})
    scenarios = []
    largest = diagnostics.get("largest_days", {}).get("comparison_after_exclusion", {})
    trimmed = (diagnostics.get("window_edges", {}).get("scenario") or {}).get("comparison", {})
    if largest.get("status") == "computed":
        scenarios.append(f"без {parameters['top_days']} найбільших днів у кожному періоді {_number(largest['change_percent'], signed=True)}%")
    if trimmed.get("status") == "computed":
        scenarios.append(f"після обрізання {parameters['trim_days']} днів із кожного краю {_number(trimmed['change_percent'], signed=True)}%")
    if scenarios:
        sentences.append("Окремі описові перевірки чутливості: " + "; ".join(scenarios) + ".")
    return " ".join(sentences)


def _method_limits(row: dict) -> list[str]:
    analysis = row.get("analysis", {})
    if not analysis:
        return []
    language = row["language"]
    limits = []
    if analysis.get("method", {}).get("seasonality_adjusted") is False:
        limits.append(f"{language}: описове порівняння середніх не є сезонно скоригованим.")
    trend = analysis.get("methodology", {}).get("trend_model", {})
    controls = trend.get("parameters", {}).get("calendar_controls", [])
    names = {"weekday": "день тижня", "month_of_year": "місяць року"}
    if controls:
        qualifier = "Окрема оцінена модель містить" if trend.get("status") == "computed" else "Для неоціненої моделі задано"
        limits.append(
            f"{language}: {qualifier.lower()} календарні змінні: {', '.join(names.get(name, name) for name in controls)}. "
            "Вони не враховують усі рухомі свята, зміни сезонних закономірностей або структурні зміни."
        )
    interval = trend.get("confidence_interval", {})
    if interval.get("status") == "computed":
        limits.append(
            f"{language}: інтервал стосується історичного нахилу лише за припущеннями моделі; "
            "це не інтервал описової відсоткової зміни, не прогноз і не тест переваги над іншими мовами."
        )
    elif interval.get("reason") == "negative_fitted_mean":
        limits.append(
            f"{language}: інтервал історичного нахилу не обчислено: лінійна модель дала від’ємні розрахункові середні "
            "для частини історичних дат. Це не реальні від’ємні перегляди й не майбутній прогноз."
        )
    elif interval:
        reason = trend.get("reason") or interval.get("reason")
        limits.append(f"{language}: інтервал історичного нахилу недоступний (причина: {reason}).")
    return limits


def render_narrative(results: dict, next_checks: list[str]) -> dict:
    rows = results["study_fresh"]["results"]
    limitations = [
        "Перегляди запитаних статей — це події, не унікальні читачі або весь інтерес до теми; вони самі по собі не доводять попиту на продукт.",
        "Історичне порівняння та описові перевірки чутливості не встановлюють причин зміни й не гарантують її продовження.",
        "Повнота календаря не доводить безпомилковість вимірювання; класифікація Wikimedia user не гарантує відсутності всіх ботів.",
    ]
    for row in rows:
        limitations.extend(_method_limits(row))
    return {
        "summary_uk": "\n\n".join(_period_text(row, results.get(f"analyze_{row['language']}", {})) for row in rows),
        "limitations_uk": limitations,
        "next_steps_uk": [NEXT_CHECKS[code] for code in next_checks],
    }