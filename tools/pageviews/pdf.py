import json
import os
from datetime import date
from importlib.util import find_spec
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from . import __version__
from .errors import PageviewsError
from .storage import read_snapshot
from .validation import ValidatedSeries, fill_missing_as_zero, validate_response

MAX_PDF_LANGUAGES = 6
PAGE_WIDTH, PAGE_HEIGHT = 8.27, 11.69  # A4 in inches
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 0.75, 0.62, 0.5
# Tried in order until the page fits; the chart is the only element that may shrink.
CHART_HEIGHTS = (2.3, 2.0, 1.75, 1.5, 1.3)
# Common Cyrillic-capable report sans-serifs; DejaVu Sans ships with Matplotlib as the last resort.
FONT_PREFERENCE = ("Arial", "Liberation Sans", "Helvetica", "PT Sans", "DejaVu Sans")
DAILY_BUCKET_MAX_DAYS = 62

INK, BODY, MUTED, RULE = "#0f172a", "#334155", "#64748b", "#cbd5e1"
PANEL, SUMMARY_BG = "#f1f5f9", "#eff6ff"
ACCENT, POSITIVE, NEGATIVE = "#1d4ed8", "#15803d", "#b91c1c"
SERIES_COLORS = ("#1d4ed8", "#c2410c", "#0f766e", "#7e22ce", "#a16207", "#be185d")
MONTHS_UK = ("січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру")
TABLE_COLUMNS = (
    ("Мова", 0.12, "left"), ("База, за день", 0.16, "right"), ("Поточний, за день", 0.18, "right"),
    ("Зміна", 0.13, "right"), ("Місяці ↓ / ↑ / =", 0.20, "right"), ("Зміна без 3 пікових днів", 0.21, "right"),
)
METHOD_NOTES = (
    "Метрика — середні щоденні перегляди статті (Wikimedia Analytics API, agent=user, access=all-access); "
    "чутливість — окремий сценарій без 3 найбільших днів кожного періоду, не статистичний тест.",
)


def _number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "н/д"
    return f"{value:,.{digits}f}".replace(",", "\u00a0").replace(".", ",").replace("-", "\u2212")


def _percent(value: float | None) -> str:
    if value is None:
        return "н/д"
    return ("+" if value > 0 else "") + _number(value) + "\u00a0%"


def _date(value: str) -> str:
    day = date.fromisoformat(value[:10])
    return f"{day.day:02d}.{day.month:02d}.{day.year}"


def _period(report: dict, name: str) -> tuple[date, date]:
    item = report["periods"][name]
    return date.fromisoformat(item["start"]), date.fromisoformat(item["end"])


def _range(period: tuple[date, date]) -> str:
    return f"{_date(period[0].isoformat())} – {_date(period[1].isoformat())}"


def _change(row: dict) -> tuple[str, float | None]:
    comparison = row.get("analysis", {}).get("comparison", {})
    value = comparison.get("change_percent") if comparison.get("status") == "computed" else None
    return _percent(value), value


def _direction_color(value: float | None) -> str:
    if value is None or value == 0:
        return MUTED
    return POSITIVE if value > 0 else NEGATIVE


def _font_family() -> str:
    from matplotlib import font_manager

    weights: dict[str, set[bool]] = {}
    for entry in font_manager.fontManager.ttflist:
        # Font collections (.ttc) are skipped: their embedding in PDF is less reliable.
        if entry.fname.lower().endswith((".ttf", ".otf")):
            weight = entry.weight if isinstance(entry.weight, int) else font_manager.weight_dict.get(entry.weight, 400)
            weights.setdefault(entry.name, set()).add(weight >= 600)
    for name in FONT_PREFERENCE:
        if weights.get(name) == {True, False}:
            return name
    return "DejaVu Sans"


def _load_series(report: dict) -> dict[str, ValidatedSeries]:
    """Re-read the study's pinned snapshots so the PDF chart never needs --chart-dir or HTTP."""
    try:
        study = json.loads(Path(report["study_path"]).expanduser().resolve().read_text(encoding="utf-8"))
        snapshots = {row["language"]: row["snapshot"] for row in study["results"] if row["status"] == "analyzed"}
        series = {}
        for language, snapshot_path in snapshots.items():
            snapshot = read_snapshot(Path(snapshot_path))
            series[language] = fill_missing_as_zero(validate_response(snapshot.response.body, snapshot.request))
    except PageviewsError as error:
        raise PageviewsError(
            "report_source_error", "A pinned snapshot failed PDF chart verification.",
            details={"source_code": error.code},
        ) from error
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise PageviewsError(
            "report_source_error", "The report's study cannot be re-read for the PDF chart.",
            details={"exception_type": type(error).__name__},
        ) from error
    return series


def _buckets(series: ValidatedSeries, start: date, end: date, daily: bool) -> tuple[list, list[float]]:
    groups: dict[object, list[int]] = {}
    for day in series.days:
        if start <= day.day <= end:
            key = day.day if daily else (day.day.year, day.day.month)
            groups.setdefault(key, []).append(day.views or 0)
    keys = sorted(groups)
    return keys, [sum(groups[key]) / len(groups[key]) for key in keys]


def _bucket_label(key: object, daily: bool) -> str:
    if daily:
        return f"{key.day:02d}.{key.month:02d}"
    year, month = key
    return f"{MONTHS_UK[month - 1]} {year % 100:02d}"


class _Page:
    """Top-down layout cursor in inches, with text measured by the real renderer."""

    def __init__(self, figure, renderer, font: str):
        self.figure, self.renderer, self.font = figure, renderer, font
        self.y = MARGIN_TOP

    @staticmethod
    def fx(inches: float) -> float:
        return inches / PAGE_WIDTH

    @staticmethod
    def fy(inches: float) -> float:
        return 1 - inches / PAGE_HEIGHT

    def width(self, text: str, size: float, weight: str = "normal") -> float:
        from matplotlib.font_manager import FontProperties

        prop = FontProperties(family=self.font, size=size, weight=weight)
        width, _, _ = self.renderer.get_text_width_height_descent(text, prop, ismath=False)
        return width / self.figure.dpi

    def wrap(self, text: str, size: float, width: float, weight: str = "normal") -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [""]:
            line = ""
            for word in paragraph.split():
                candidate = f"{line} {word}" if line else word
                if line and self.width(candidate, size, weight) > width:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            lines.append(line)
        return lines

    def fit(self, text: str, size: float, width: float, weight: str = "normal") -> str:
        if self.width(text, size, weight) <= width:
            return text
        while text and self.width(text + "…", size, weight) > width:
            text = text[:-1]
        return text.rstrip() + "…"

    def text(self, x: float, y: float, value: str, size: float, *, weight: str = "normal",
             color: str = BODY, ha: str = "left", url: str | None = None) -> None:
        self.figure.text(self.fx(x), self.fy(y), value, fontsize=size, fontweight=weight, color=color,
                         ha=ha, va="top", family=self.font, url=url)

    def lines(self, lines: list[str], x: float, y: float, size: float, *, leading: float = 1.38,
              **style) -> float:
        step = size * leading / 72
        for index, line in enumerate(lines):
            self.text(x, y + index * step, line, size, **style)
        return len(lines) * step

    def paragraph(self, value: str, size: float, *, x: float = MARGIN_X, width: float | None = None,
                  leading: float = 1.38, weight: str = "normal", color: str = BODY) -> None:
        width = PAGE_WIDTH - MARGIN_X - x if width is None else width
        lines = self.wrap(value, size, width, weight)
        self.y += self.lines(lines, x, self.y, size, leading=leading, weight=weight, color=color)

    def bullets(self, items: list[str], size: float, *, x: float = MARGIN_X, width: float | None = None,
                color: str = BODY, leading: float = 1.3) -> float:
        width = PAGE_WIDTH - MARGIN_X - x if width is None else width
        start = self.y
        for item in items:
            self.text(x, self.y, "•", size, color=MUTED)
            self.paragraph(item, size, x=x + 0.13, width=width - 0.13, leading=leading, color=color)
            self.y += size * 0.25 / 72
        return self.y - start

    def rect(self, x: float, y: float, width: float, height: float, color: str) -> None:
        from matplotlib.patches import Rectangle

        self.figure.add_artist(Rectangle(
            (self.fx(x), self.fy(y + height)), width / PAGE_WIDTH, height / PAGE_HEIGHT,
            transform=self.figure.transFigure, facecolor=color, edgecolor="none",
        ))

    def rule(self, y: float | None = None, *, color: str = RULE, linewidth: float = 0.6) -> None:
        from matplotlib.lines import Line2D

        y = self.y if y is None else y
        self.figure.add_artist(Line2D(
            [self.fx(MARGIN_X), self.fx(PAGE_WIDTH - MARGIN_X)], [self.fy(y), self.fy(y)],
            transform=self.figure.transFigure, color=color, linewidth=linewidth,
        ))

    def heading(self, title: str) -> None:
        self.y += 0.13
        self.text(MARGIN_X, self.y, title.upper(), 7.5, weight="bold", color=ACCENT)
        self.y += 0.14
        self.rule()
        self.y += 0.07


def _header(page: _Page, report: dict) -> None:
    content_width = PAGE_WIDTH - 2 * MARGIN_X
    page.rect(0, 0, PAGE_WIDTH, 0.09, ACCENT)
    page.text(MARGIN_X, page.y, "TREND-VISOR  ·  ЗВІТ ПРО ПЕРЕГЛЯДИ WIKIPEDIA", 7.2, weight="bold", color=ACCENT)
    page.text(PAGE_WIDTH - MARGIN_X, page.y, f"Станом на {_date(report['as_of'])} (UTC)", 7.2,
              color=MUTED, ha="right")
    page.y += 0.25
    page.paragraph(report["question"], 16, width=content_width, leading=1.22, weight="bold", color=INK)
    page.y += 0.07
    languages = ", ".join(row["language"] for row in report["evidence"])
    page.paragraph(
        f"Базовий період: {_range(_period(report, 'baseline'))}   ·   "
        f"Поточний період: {_range(_period(report, 'current'))}   ·   Мовні розділи: {languages}",
        8, width=content_width, color=MUTED,
    )
    if report["criteria"]:
        page.paragraph("Критерії користувача: " + "; ".join(report["criteria"]), 8, width=content_width, color=MUTED)
    page.y += 0.1
    page.rule(color=INK, linewidth=0.8)
    page.y += 0.16


def _cards(page: _Page, rows: list[dict]) -> None:
    columns, gap, height = min(len(rows), 3), 0.14, 0.95
    width = (PAGE_WIDTH - 2 * MARGIN_X - gap * (columns - 1)) / columns
    for start in range(0, len(rows), columns):
        for index, row in enumerate(rows[start:start + columns]):
            x, y = MARGIN_X + index * (width + gap), page.y
            text, value = _change(row)
            color = _direction_color(value)
            inner, inner_width = x + 0.17, width - 0.27
            page.rect(x, y, width, height, PANEL)
            page.rect(x, y, 0.045, height, color)
            label = page.fit(f"{row['language'].upper()}  ·  {row['article']}", 7.3, inner_width, "bold")
            page.text(inner, y + 0.12, label, 7.3, weight="bold", color=MUTED)
            page.text(inner, y + 0.29, text, 20, weight="bold", color=color)
            if row["status"] != "analyzed":
                detail, note = "Дані не зібрано", f"причина: {row['reason']}"
            else:
                analysis = row["analysis"]
                detail = (f"{_number(analysis['baseline']['mean_daily_views_observed'])} → "
                          f"{_number(analysis['current']['mean_daily_views_observed'])} переглядів за день")
                reason = analysis["comparison"].get("reason")
                note = f"зміну не обчислено: {reason}" if value is None else "середнє: базовий → поточний період"
            page.text(inner, y + 0.64, page.fit(detail, 7.5, inner_width), 7.5, color=BODY)
            page.text(inner, y + 0.79, page.fit(note, 6.8, inner_width), 6.8, color=MUTED)
        page.y += height + gap


def _conclusion(page: _Page, summary: str) -> None:
    page.heading("Висновок")
    pad, content_width = 0.13, PAGE_WIDTH - 2 * MARGIN_X
    text_x, text_width = MARGIN_X + pad + 0.05, content_width - 2 * pad - 0.05
    lines = page.wrap(summary, 9, text_width)
    note = page.wrap("Сформульовано агентом на основі даних цього звіту; числа на картках, "
                     "графіку й у таблиці перераховано кодом зі збережених знімків.", 6.8, text_width)
    height = 2 * pad + len(lines) * 9 * 1.4 / 72 + 0.05 + len(note) * 6.8 * 1.35 / 72
    page.rect(MARGIN_X, page.y, content_width, height, SUMMARY_BG)
    page.rect(MARGIN_X, page.y, 0.045, height, ACCENT)
    y = page.y + pad
    y += page.lines(lines, text_x, y, 9, leading=1.4, color=INK) + 0.05
    page.lines(note, text_x, y, 6.8, leading=1.35, color=MUTED)
    page.y += height


def _chart(page: _Page, report: dict, series: dict[str, ValidatedSeries], height: float) -> None:
    from matplotlib.font_manager import FontProperties
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    page.heading("Динаміка переглядів")
    rows = [row for row in report["evidence"] if row["language"] in series]
    if not rows:
        page.paragraph("Графік недоступний: жодну статтю не проаналізовано.", 8, color=MUTED)
        return
    (baseline_start, baseline_end), (current_start, current_end) = _period(report, "baseline"), _period(report, "current")
    daily = (current_end - current_start).days + 1 <= DAILY_BUCKET_MAX_DAYS
    indexed = len(rows) > 1
    legend_space, axis_left = 0.24, 0.52
    axes = page.figure.add_axes((
        page.fx(MARGIN_X + axis_left), page.fy(page.y + legend_space + height),
        (PAGE_WIDTH - 2 * MARGIN_X - axis_left - 0.05) / PAGE_WIDTH, height / PAGE_HEIGHT,
    ))
    labels: list = []
    handles = []
    for index, row in enumerate(rows):
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        base_mean = row["analysis"]["baseline"]["mean_daily_views_observed"]
        if indexed and not base_mean:
            continue
        scale = 100 / base_mean if indexed else 1
        _, base_values = _buckets(series[row["language"]], baseline_start, baseline_end, daily)
        current_keys, current_values = _buckets(series[row["language"]], current_start, current_end, daily)
        axes.plot(range(len(base_values)), [value * scale for value in base_values],
                  color=color, linewidth=1.1, linestyle=(0, (3, 2)), alpha=0.7)
        axes.plot(range(len(current_values)), [value * scale for value in current_values], color=color,
                  linewidth=1.8, marker="o" if len(current_values) <= 36 else None, markersize=2.6)
        labels = current_keys if len(current_keys) > len(labels) else labels
        if indexed:
            handles.append(Line2D([], [], color=color, linewidth=1.8, label=row["language"].upper()))
    if indexed:
        axes.axhline(100, color=MUTED, linewidth=0.7, linestyle=(0, (1, 2)))
        handles += [Line2D([], [], color=MUTED, linewidth=1.8, label="поточний період"),
                    Line2D([], [], color=MUTED, linewidth=1.1, linestyle=(0, (3, 2)), label="базовий період")]
        axes.set_ylabel("Індекс, база = 100", fontsize=7.5, color=MUTED)
    else:
        color = SERIES_COLORS[0]
        handles = [
            Line2D([], [], color=color, linewidth=1.8, label=f"Поточний: {_range((current_start, current_end))}"),
            Line2D([], [], color=color, linewidth=1.1, linestyle=(0, (3, 2)), alpha=0.7,
                   label=f"Базовий: {_range((baseline_start, baseline_end))}"),
        ]
        axes.set_ylabel("Переглядів за день", fontsize=7.5, color=MUTED)
    step = max(1, ceil(len(labels) / 12))
    axes.set_xticks(range(0, len(labels), step), [_bucket_label(key, daily) for key in labels[::step]])
    axes.set_ylim(bottom=0)
    axes.yaxis.set_major_locator(MaxNLocator(nbins=5))
    axes.yaxis.set_major_formatter(FuncFormatter(lambda value, _: _number(value, 0)))
    axes.tick_params(labelsize=7, colors=MUTED, length=2.5)
    axes.grid(axis="y", color="#e2e8f0", linewidth=0.5)
    axes.set_axisbelow(True)
    axes.spines[["top", "right"]].set_visible(False)
    axes.spines[["left", "bottom"]].set_color(RULE)
    axes.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.01), ncols=min(len(handles), 4),
                frameon=False, prop=FontProperties(family=page.font, size=7.2), handlelength=2.4,
                borderaxespad=0, columnspacing=1.4)
    page.y += legend_space + height + 0.3
    unit = "день" if daily else "календарний місяць"
    caption = (f"Середні щоденні перегляди за кожен {unit}; поточний період накладено на базовий за порядковим "
               f"номером {'дня' if daily else 'місяця'}. Дні без рядка API пораховано як 0.")
    if indexed:
        caption += (" Значення нормовано: середнє базового періоду кожної мови = 100, бо абсолютні рівні "
                    "різних мовних розділів не порівнювані.")
    page.paragraph(caption, 6.8, leading=1.35, color=MUTED)


def _table_cells(row: dict) -> list[tuple[str, str]]:
    if row["status"] != "analyzed":
        return [(row["language"], INK), ("—", MUTED), ("—", MUTED), ("не зібрано", MUTED), ("—", MUTED), ("—", MUTED)]
    analysis = row["analysis"]
    change, value = _change(row)
    calendar = row.get("calendar", {}).get("summary", {})
    months = (f"{calendar['lower_pairs']} / {calendar['higher_pairs']} / {calendar['equal_pairs']}"
              if calendar.get("computed_pairs") else "н/д")
    spikes = row.get("diagnostics", {}).get("largest_days", {}).get("comparison_after_exclusion", {})
    spike_value = spikes.get("change_percent") if spikes.get("status") == "computed" else None
    return [
        (row["language"], INK),
        (_number(analysis["baseline"]["mean_daily_views_observed"]), BODY),
        (_number(analysis["current"]["mean_daily_views_observed"]), BODY),
        (change, _direction_color(value)),
        (months, BODY),
        (_percent(spike_value), _direction_color(spike_value)),
    ]


def _table(page: _Page, rows: list[dict]) -> None:
    page.heading("Ключові показники")
    content_width = PAGE_WIDTH - 2 * MARGIN_X
    row_height = 0.22

    def draw(cells: list[tuple[str, str]], size: float, weight: str) -> None:
        x = MARGIN_X
        for (value, color), (_, share, align) in zip(cells, TABLE_COLUMNS):
            width = share * content_width
            text = page.fit(value, size, width - 0.08, weight)
            anchor = x + width - 0.04 if align == "right" else x + 0.04
            page.text(anchor, page.y + 0.055, text, size, weight=weight, color=color, ha=align)
            x += width

    draw([(title, MUTED) for title, _, _ in TABLE_COLUMNS], 7.2, "bold")
    page.y += row_height
    page.rule(color=INK, linewidth=0.7)
    for index, row in enumerate(rows):
        if index % 2:
            page.rect(MARGIN_X, page.y, content_width, row_height, PANEL)
        draw(_table_cells(row), 8, "normal")
        page.y += row_height
    page.rule()
    page.y += 0.07
    page.paragraph("Місяці — пари того самого місяця рік тому: нижче / вище / без змін; н/д — таких пар немає.",
                   6.8, leading=1.35, color=MUTED)


def _next_checks(page: _Page, rows: list[dict]) -> None:
    page.heading("Що перевірити далі")
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["next_check"]["text_uk"], []).append(row["language"])
    items = [f"{', '.join(languages)}: {text}" for text, languages in grouped.items()]
    items.append("Попит на продукт перевіряти окремо з його користувачами; Wikipedia лише допомагає сформувати гіпотези.")
    page.bullets(items, 8)


def _method(page: _Page, limitations: list[str]) -> None:
    page.heading("Метод і обмеження")
    items = [*METHOD_NOTES, *limitations]
    gap = 0.25
    width = (PAGE_WIDTH - 2 * MARGIN_X - gap) / 2
    middle = ceil(len(items) / 2)
    start = page.y
    left = page.bullets(items[:middle], 6.8, width=width, color=MUTED, leading=1.25)
    page.y = start
    right = page.bullets(items[middle:], 6.8, x=MARGIN_X + width + gap, width=width, color=MUTED, leading=1.25)
    page.y = start + max(left, right)


def _footer(page: _Page, report: dict) -> float:
    """Draw the fixed footer and return the lowest y the flowing content may reach."""
    content_width = PAGE_WIDTH - 2 * MARGIN_X
    lines: list[tuple[str, str | None]] = []
    for row in report["evidence"]:
        source = row.get("source")
        if source is not None:
            url = f"https://{row['project']}/wiki/{quote(row['article'].replace(' ', '_'))}"
            lines.append((f"Джерело ({row['language']}): «{row['article']}», {row['project']} — Wikimedia "
                          f"Analytics API, дані отримано {_date(source['fetched_at_utc'])}", url))
    lines.append((f"trend-visor {__version__}  ·  звіт {report['report_id'][:12]}  ·  перераховано офлайн зі "
                  "збережених знімків; повні дані й контрольні суми — у JSON/Markdown-звіті", None))
    step = 6.5 * 1.4 / 72
    top = PAGE_HEIGHT - MARGIN_BOTTOM - len(lines) * step
    page.rule(top - 0.07)
    for index, (text, url) in enumerate(lines):
        page.text(MARGIN_X, top + index * step, page.fit(text, 6.5, content_width), 6.5, color=MUTED, url=url)
    return top - 0.2


def _compose(page: _Page, report: dict, series: dict[str, ValidatedSeries], chart_height: float) -> bool:
    limit = _footer(page, report)
    _header(page, report)
    _cards(page, report["evidence"])
    if report.get("summary_uk"):
        _conclusion(page, report["summary_uk"])
    _chart(page, report, series, chart_height)
    _table(page, report["evidence"])
    _next_checks(page, report["evidence"])
    _method(page, report["limitations_uk"])
    return page.y <= limit


def render_report_pdf(report: dict, output: Path) -> Path:
    """Render an already-built report to a single A4 page with a vector chart drawn
    from the pinned snapshots; refuses instead of silently dropping or overlapping content."""
    if find_spec("matplotlib") is None:
        raise PageviewsError("missing_dependency", "Install the charts extra before rendering a PDF.")
    if len(report["evidence"]) > MAX_PDF_LANGUAGES:
        raise PageviewsError(
            "pdf_one_page_not_eligible",
            f"A one-page PDF supports at most {MAX_PDF_LANGUAGES} languages; use the Markdown report instead.",
        )
    output = output.expanduser()
    if output.is_symlink():
        raise PageviewsError("output_exists", "Output path is a symlink; choose a new path.")
    output = output.resolve()
    if output.suffix.lower() != ".pdf":
        raise PageviewsError("invalid_request", "The PDF output must use a .pdf extension.")
    if output.exists():
        raise PageviewsError("output_exists", "PDF output already exists; choose a new filename.")

    series = _load_series(report)
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    font = _font_family()
    # Type 42 embeds real TrueType fonts, so text stays selectable and searchable.
    style = {"font.family": font, "pdf.fonttype": 42, "text.usetex": False,
             "text.parse_math": False, "axes.unicode_minus": True}
    metadata = {
        "Title": report["question"], "Author": "trend-visor", "Creator": f"trend-visor {__version__}",
        "Subject": "Wikipedia pageviews report",
        "Keywords": "Wikipedia, pageviews, " + ", ".join(row["language"] for row in report["evidence"]),
        "CreationDate": None,
    }
    with rc_context(style):
        figure = Figure(figsize=(PAGE_WIDTH, PAGE_HEIGHT), dpi=72)
        renderer = FigureCanvasAgg(figure).get_renderer()
        for chart_height in CHART_HEIGHTS:
            figure.clear()
            figure.set_facecolor("white")
            if _compose(_Page(figure, renderer, font), report, series, chart_height):
                break
        else:
            raise PageviewsError(
                "pdf_one_page_not_eligible",
                "This report's text and chart do not fit on one A4 page without dropping or "
                "overlapping content; shorten the question/criteria/summary or use the Markdown report.",
            )
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".report-pdf-", dir=output.parent) as temporary:
                prepared = Path(temporary) / "report.pdf"
                figure.savefig(prepared, format="pdf", metadata=metadata)
                # Linking publishes a complete file without replacing an existing destination.
                os.link(prepared, output)
        except FileExistsError as error:
            raise PageviewsError("output_exists", "PDF output already exists; choose a new filename.") from error
        except OSError as error:
            raise PageviewsError(
                "artifact_write_error", "Could not save the PDF; check path, permissions and disk space.",
                details={"exception_type": type(error).__name__},
            ) from error
    return output
