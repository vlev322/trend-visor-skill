from fractions import Fraction


def period_direction(analysis: dict) -> str:
    if analysis.get("comparison", {}).get("status") != "computed":
        return "not_computed"
    baseline, current = analysis["baseline"], analysis["current"]
    before = Fraction(baseline["sum_observed_views"], baseline["coverage"]["observed_days"])
    after = Fraction(current["sum_observed_views"], current["coverage"]["observed_days"])
    return "increase" if after > before else ("decrease" if after < before else "unchanged")


def format_number(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "невідомо"
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return text.replace(",", " ").replace(".", ",").replace("-", "−")


def period_text(row: dict, detailed: dict) -> str:
    language = row["language"]
    if row["status"] != "analyzed":
        return f"{language}: аналіз статті недоступний ({row['reason']}); це не доказ відсутності інтересу."
    analysis = row["analysis"]
    baseline, current = analysis["baseline"], analysis["current"]
    before = f"{baseline['window']['start']}–{baseline['window']['end']}"
    after = f"{current['window']['start']}–{current['window']['end']}"
    title = row.get("article") or language
    sentences = [f"Стаття «{title}» ({row.get('project', language)}), періоди {before} та {after}."]
    direction = period_direction(analysis)
    means = (format_number(baseline["mean_daily_views_observed"]), format_number(current["mean_daily_views_observed"]))
    if direction == "not_computed":
        reason = analysis["comparison"]["reason"]
        sentences.append(f"Середні за спостережені дні — {means[0]} та {means[1]} на день; відносну зміну не обчислено ({reason}).")
    else:
        verb = {"increase": "зросли", "decrease": "знизилися", "unchanged": "не змінилися"}[direction]
        change = format_number(analysis["comparison"]["change_percent"], signed=True)
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
        scenarios.append(f"без {parameters['top_days']} найбільших днів у кожному періоді {format_number(largest['change_percent'], signed=True)}%")
    if trimmed.get("status") == "computed":
        scenarios.append(f"після обрізання {parameters['trim_days']} днів із кожного краю {format_number(trimmed['change_percent'], signed=True)}%")
    if scenarios:
        sentences.append("Окремі описові перевірки чутливості: " + "; ".join(scenarios) + ".")
    return " ".join(sentences)
