from tools.pageviews.narrative import period_direction as _direction, period_text as _period_text

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


def interpretations(results: dict) -> list[dict]:
    return [{
        "language": row["language"], "period_change_direction": _direction(row.get("analysis", {})),
        "trend_interval_available": row.get("analysis", {}).get("methodology", {}).get(
            "trend_model", {},
        ).get("confidence_interval", {}).get("status") == "computed",
    } for row in results["study_fresh"]["results"]]


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