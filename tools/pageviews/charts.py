import json
import os
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from math import isfinite, nan
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import fill
from typing import TYPE_CHECKING

from .analysis import Period, PeriodSummary, summarize_period, validate_period_order
from .errors import PageviewsError
from .validation import ValidatedSeries

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

CHART_VERSION = 1
CHART_DPI = 150
PERIOD_COLORS = ("#2563eb", "#c16808")
MISSING_COLOR = "#c93644"
STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "timezone": "UTC",
    "text.usetex": False,
    "text.parse_math": False,
    "path.simplify": False,
    "axes.edgecolor": "#cbd5e1",
    "axes.labelcolor": "#334155",
    "xtick.color": "#475569",
    "ytick.color": "#475569",
}


@dataclass(frozen=True, slots=True)
class PlotPeriod:
    name: str
    dates: tuple[date, ...]
    values: tuple[float, ...]
    missing_dates: tuple[date, ...]
    summary: PeriodSummary


@dataclass(frozen=True, slots=True)
class ChartSource:
    article: str
    project: str
    url: str
    fetched_at: str
    response_sha256: str


def _prepare_period(
    series: ValidatedSeries, period: Period, name: str
) -> PlotPeriod:
    summary = summarize_period(series, period)
    selected = [day for day in series.days if period.start <= day.day <= period.end]
    return PlotPeriod(
        name=name,
        dates=tuple(day.day for day in selected),
        values=tuple(nan if day.views is None else day.views for day in selected),
        missing_dates=tuple(day.day for day in selected if day.views is None),
        summary=summary,
    )


def prepare_chart_data(
    series: ValidatedSeries, baseline_period: Period, current_period: Period
) -> tuple[PlotPeriod, PlotPeriod]:
    validate_period_order(baseline_period, current_period)
    return (
        _prepare_period(series, baseline_period, "Baseline"),
        _prepare_period(series, current_period, "Current"),
    )


def _draw_period(axes: "Axes", period: PlotPeriod, color: str) -> None:
    from matplotlib.dates import date2num

    dates = date2num(period.dates)
    window = period.summary.period
    axes.axvspan(dates[0] - 0.5, dates[-1] + 0.5, color=color, alpha=0.045)
    axes.plot(
        dates,
        period.values,
        color=color,
        linewidth=1.2,
        marker="o",
        markersize=2.5,
        clip_on=False,
        label=f"{period.name}: {window.start.isoformat()} to {window.end.isoformat()}",
        gid=f"{period.name.lower()}-views",
    )
    if period.missing_dates:
        axes.plot(
            date2num(period.missing_dates),
            [1.015] * len(period.missing_dates),
            transform=axes.get_xaxis_transform(),
            linestyle="none",
            marker="|",
            markersize=10,
            color=MISSING_COLOR,
            clip_on=False,
            gid=f"{period.name.lower()}-missing",
        )


def _draw_footer(
    axes: "Axes", periods: tuple[PlotPeriod, PlotPeriod], source: ChartSource
) -> None:
    axes.set_axis_off()
    for position, period, color in zip((0.95, 0.77), periods, PERIOD_COLORS):
        summary = period.summary
        axes.text(
            0,
            position,
            f"{period.name} coverage: {summary.observed_days}/{summary.expected_days} "
            f"days had an API row  |  No API row (counted as 0): {summary.missing_days}  |  "
            f"Explicit zeros: {summary.explicit_zero_days}",
            transform=axes.transAxes,
            color=color,
            fontsize=10,
            va="top",
        )
    notes = (
        (0.55, "Raw daily values; no smoothing. Wikimedia omits zero-view days: "
         "line gaps and red ticks above the plot mark days counted as 0 here."),
        (0.37, "Pageviews are events, not unique people or evidence of purchase intent."),
        (0.19, f"Source: Wikimedia Analytics API  |  Fetched (UTC): {source.fetched_at}"),
        (0.01, f"Raw SHA-256: {source.response_sha256}"),
    )
    for position, text in notes:
        axes.text(
            0, position, text, transform=axes.transAxes,
            fontsize=8.5, color="#475569", va="top",
        )


def build_figure(
    periods: tuple[PlotPeriod, PlotPeriod], source: ChartSource
) -> "Figure":
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.dates import (
            AutoDateLocator, ConciseDateFormatter, DateFormatter, DayLocator, date2num,
        )
        from matplotlib.figure import Figure
        from matplotlib.ticker import MaxNLocator
    except ImportError as error:
        raise PageviewsError(
            "missing_dependency",
            "Charts require Matplotlib. Run 'uv sync --locked --extra charts' "
            "and use the .venv Python interpreter.",
        ) from error

    with rc_context(STYLE):
        title = fill(source.article, width=72)
        height = 8 + 0.25 * title.count("\n")
        figure = Figure(figsize=(12, height), dpi=CHART_DPI, layout="constrained")
        FigureCanvasAgg(figure)
        figure.set_facecolor("white")
        figure.suptitle(title, fontsize=18, fontweight="bold", color="#0f172a")
        grid = figure.add_gridspec(2, 1, height_ratios=(4.8, 1.5))
        axes = figure.add_subplot(grid[0])
        footer = figure.add_subplot(grid[1])
        axes.set_title(
            f"Daily Wikipedia pageviews  |  {source.project}  |  user / all-access",
            loc="left", fontsize=10, color="#475569", pad=20,
        )
        for period, color in zip(periods, PERIOD_COLORS):
            _draw_period(axes, period, color)

        baseline, current = periods
        start = date2num(baseline.summary.period.start)
        end = date2num(current.summary.period.end)
        gap_start = date2num(baseline.summary.period.end) + 0.5
        gap_end = date2num(current.summary.period.start) - 0.5
        if gap_start < gap_end:
            axes.axvspan(gap_start, gap_end, color="#94a3b8", alpha=0.2)
            axes.text(
                (gap_start + gap_end) / 2, 0.5, "Outside selected periods",
                transform=axes.get_xaxis_transform(), rotation=90,
                ha="center", va="center", fontsize=8, color="#475569",
            )
        axes.set_xlim(start - 0.5, end + 0.5)
        values = [
            value for period in periods for value in period.values if isfinite(value)
        ]
        axes.set_ylim(0, max(1, max(values, default=0) * 1.12))
        axes.set_xlabel("Date (UTC)", labelpad=8)
        axes.set_ylabel("Pageviews per day")
        axes.yaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))
        if end - start <= 7:
            axes.xaxis.set_major_locator(DayLocator(tz="UTC"))
            axes.xaxis.set_major_formatter(DateFormatter("%Y-%m-%d", tz="UTC"))
        else:
            locator = AutoDateLocator(minticks=3, maxticks=8, tz="UTC")
            axes.xaxis.set_major_locator(locator)
            axes.xaxis.set_major_formatter(ConciseDateFormatter(locator, tz="UTC"))
        axes.grid(axis="y", color="#e2e8f0", linewidth=0.7)
        axes.set_axisbelow(True)
        axes.spines[["top", "right"]].set_visible(False)
        axes.legend(loc="upper right", fontsize=9, framealpha=0.95)
        if not values:
            axes.text(
                0.5, 0.5, "No observations in the selected periods",
                transform=axes.transAxes, ha="center", color="#475569",
            )
        _draw_footer(footer, periods, source)
        return figure


def render_png(periods: tuple[PlotPeriod, PlotPeriod], source: ChartSource) -> bytes:
    figure = build_figure(periods, source)
    from matplotlib import __version__ as matplotlib_version, rc_context

    description = {
        "chart_version": CHART_VERSION,
        "article": source.article,
        "project": source.project,
        "agent": "user",
        "access": "all-access",
        "fetched_at_utc": source.fetched_at,
        "response_sha256": source.response_sha256,
        "smoothing": None,
        "missing_values": "line_gaps",
        "periods": {
            period.name.lower(): period.summary.as_dict() for period in periods
        },
    }
    metadata = {
        "Title": source.article,
        "Source": source.url,
        "Description": json.dumps(description, ensure_ascii=False, allow_nan=False),
        "Software": f"trend-visor chart v{CHART_VERSION}; Matplotlib {matplotlib_version}",
    }
    try:
        with rc_context(STYLE), BytesIO() as buffer:
            figure.savefig(buffer, format="png", dpi=CHART_DPI, metadata=metadata)
            return buffer.getvalue()
    finally:
        figure.clear()


def save_png(png: bytes, output: Path) -> Path:
    output = output.expanduser()
    if output.is_symlink():
        raise PageviewsError(
            "output_exists", "Output path is a symlink; choose a new path."
        )
    output = output.resolve()
    if output.suffix.lower() != ".png":
        raise PageviewsError(
            "invalid_request", "The chart output must use a .png extension."
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".chart-", dir=output.parent) as temporary:
            prepared = Path(temporary) / "chart.png"
            prepared.write_bytes(png)
            # Linking publishes a complete file without replacing an existing path.
            os.link(prepared, output)
    except FileExistsError as error:
        raise PageviewsError(
            "output_exists", "Chart output already exists; choose a new filename.",
            details={"path": str(output)},
        ) from error
    except OSError as error:
        raise PageviewsError(
            "chart_write_error", "Could not save the PNG; check path and permissions.",
            details={"path": str(output), "exception_type": type(error).__name__},
        ) from error
    return output