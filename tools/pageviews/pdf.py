import os
import textwrap
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import PageviewsError
from .narrative import diagnostic_text, period_text

MAX_PDF_LANGUAGES = 3
WRAP_WIDTH = 108
LINE_HEIGHT = 0.0135
IMAGE_HEIGHT = 0.20
TOP_MARGIN = 0.97
BOTTOM_MARGIN = 0.035


def _wrapped(text: str, *, width: int = WRAP_WIDTH) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=width) or [""])
    return lines


def _bulleted(items: list[str], *, width: int = WRAP_WIDTH) -> list[str]:
    lines: list[str] = []
    for item in items:
        lines.extend(textwrap.wrap(item, width=width, initial_indent="• ", subsequent_indent="  ") or ["• "])
    return lines


def _blocks(report: dict) -> list[tuple[str, list[str], bool]]:
    """Return (heading, body_lines, has_chart) blocks in the order they are drawn."""
    baseline, current = report["periods"]["baseline"], report["periods"]["current"]
    blocks = [(
        "Звіт про перегляди Wikipedia", _wrapped(report["question"]), False,
    ), (
        None,
        [f"Базовий період: {baseline['start']}–{baseline['end']}; "
         f"поточний: {current['start']}–{current['end']}; as-of {report['as_of']} UTC."],
        False,
    )]
    for row in report["evidence"]:
        lines = _wrapped(period_text(row))
        if "diagnostics" in row:
            lines += _wrapped(diagnostic_text(row["diagnostics"]))
        check = row["next_check"]
        lines += _wrapped(f"Далі: {check['text_uk']}")
        blocks.append((f"{row['language']}", lines, "chart" in row))
    if report.get("summary_uk"):
        blocks.append(("Висновок агента", _wrapped(report["summary_uk"]), False))
    blocks.append(("Обмеження", _bulleted(report["limitations_uk"]), False))
    sources = [
        f"{row['language']}: {row['source']['url']} (отримано {row['source']['fetched_at_utc']})"
        for row in report["evidence"] if row.get("source") is not None
    ]
    if sources:
        blocks.append(("Джерела", _bulleted(sources), False))
    return blocks


def _required_height(blocks: list[tuple[str, list[str], bool]]) -> float:
    total = 0.0
    for heading, lines, has_chart in blocks:
        if heading is not None:
            total += LINE_HEIGHT * 1.6
        total += LINE_HEIGHT * len(lines)
        if has_chart:
            total += IMAGE_HEIGHT
    return total


def render_report_pdf(report: dict, output: Path) -> Path:
    """Render an already-built report to a single A4 page, reusing its verified
    charts as-is; refuses instead of silently dropping content or overlapping text."""
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

    blocks = _blocks(report)
    available = TOP_MARGIN - BOTTOM_MARGIN
    if _required_height(blocks) > available:
        raise PageviewsError(
            "pdf_one_page_not_eligible",
            "This report's text and charts do not fit on one A4 page without dropping or "
            "overlapping content; shorten the question/criteria/summary or use the Markdown report.",
        )

    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.image import imread

    figure = plt.figure(figsize=(8.27, 11.69), dpi=150)
    try:
        y = TOP_MARGIN
        for heading, lines, has_chart in blocks:
            if heading is not None:
                figure.text(0.06, y, heading, fontsize=13, fontweight="bold", va="top")
                y -= LINE_HEIGHT * 1.6
            if lines:
                figure.text(0.06, y, "\n".join(lines), fontsize=8, va="top", linespacing=1.5)
                y -= LINE_HEIGHT * len(lines)
            if has_chart:
                row = next(row for row in report["evidence"] if row.get("language") == heading and "chart" in row)
                axes = figure.add_axes((0.08, y - IMAGE_HEIGHT, 0.84, IMAGE_HEIGHT))
                axes.imshow(imread(row["chart"]["path"]))
                axes.axis("off")
                y -= IMAGE_HEIGHT
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".report-pdf-", dir=output.parent) as temporary:
            prepared = Path(temporary) / "report.pdf"
            figure.savefig(prepared, format="pdf")
            # Linking publishes a complete file without replacing an existing destination.
            os.link(prepared, output)
    except FileExistsError as error:
        raise PageviewsError("output_exists", "PDF output already exists; choose a new filename.") from error
    except OSError as error:
        raise PageviewsError(
            "artifact_write_error", "Could not save the PDF; check path, permissions and disk space.",
            details={"exception_type": type(error).__name__},
        ) from error
    finally:
        plt.close(figure)
    return output
