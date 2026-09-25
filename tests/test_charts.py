import io
import json
import math
import unittest
from dataclasses import replace
from datetime import date, timedelta
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.pageviews.analysis import Period
from tools.pageviews.charts import (
    ChartSource,
    build_figure,
    prepare_chart_data,
    render_png,
    save_png,
)
from tools.pageviews.errors import PageviewsError
from tools.pageviews.models import DailyViews
from tools.pageviews.validation import ValidatedSeries


def make_series(values, start=date(2026, 1, 1)):
    return ValidatedSeries(
        days=tuple(
            DailyViews(start + timedelta(days=index), value)
            for index, value in enumerate(values)
        )
    )


class ChartDataTests(unittest.TestCase):
    def test_missing_values_break_lines_and_observed_zeros_are_kept(self):
        series = ValidatedSeries(
            days=(
                DailyViews(date(2026, 7, 12), 10),
                DailyViews(date(2026, 7, 13), None),
                DailyViews(date(2026, 7, 14), 0),
                DailyViews(date(2026, 7, 15), 20),
            )
        )
        baseline = Period(date(2026, 7, 12), date(2026, 7, 14))
        current = Period(date(2026, 7, 15), date(2026, 7, 15))

        first, second = prepare_chart_data(series, baseline, current)

        self.assertEqual(first.dates, tuple(day.day for day in series.days[:3]))
        self.assertEqual(first.values[0], 10)
        self.assertTrue(math.isnan(first.values[1]))
        self.assertEqual(first.values[2], 0)
        self.assertEqual(first.missing_dates, (date(2026, 7, 13),))
        self.assertEqual(first.summary.observed_days, 2)
        self.assertEqual(first.summary.explicit_zero_days, 1)
        self.assertEqual(second.values, (20,))
        self.assertIsNone(series.days[1].views)

    def test_unselected_dates_are_not_plotted_or_counted_as_missing(self):
        series = make_series([1, 2, 999, None, 0, 3])
        first, second = prepare_chart_data(
            series,
            Period(date(2026, 1, 1), date(2026, 1, 2)),
            Period(date(2026, 1, 5), date(2026, 1, 6)),
        )
        self.assertEqual(first.values, (1, 2))
        self.assertEqual(second.values, (0, 3))
        self.assertEqual(first.missing_dates + second.missing_dates, ())

    def test_single_day_periods_keep_the_leap_day(self):
        series = make_series([1, 0, 2], start=date(2024, 2, 28))
        first, second = prepare_chart_data(
            series,
            Period(date(2024, 2, 29), date(2024, 2, 29)),
            Period(date(2024, 3, 1), date(2024, 3, 1)),
        )
        self.assertEqual(first.dates, (date(2024, 2, 29),))
        self.assertEqual(first.values, (0,))
        self.assertEqual(second.dates, (date(2024, 3, 1),))

    def test_rejects_overlapping_and_out_of_snapshot_periods(self):
        series = make_series([1, 2, 3])
        cases = (
            (Period(date(2026, 1, 1), date(2026, 1, 2)),
             Period(date(2026, 1, 2), date(2026, 1, 3))),
            (Period(date(2025, 12, 31), date(2026, 1, 1)),
             Period(date(2026, 1, 2), date(2026, 1, 3))),
        )
        for baseline, current in cases:
            with self.subTest(baseline=baseline, current=current):
                with self.assertRaises(PageviewsError) as caught:
                    prepare_chart_data(series, baseline, current)
                self.assertEqual(caught.exception.code, "invalid_request")


@unittest.skipUnless(find_spec("matplotlib"), "Install the charts extra to render PNGs")
class ChartRenderingTests(unittest.TestCase):
    def setUp(self):
        series = ValidatedSeries(
            days=(
                DailyViews(date(2026, 7, 12), 10),
                DailyViews(date(2026, 7, 13), None),
                DailyViews(date(2026, 7, 14), 0),
                DailyViews(date(2026, 7, 15), 20),
                DailyViews(date(2026, 7, 16), 30),
            )
        )
        self.periods = prepare_chart_data(
            series,
            Period(date(2026, 7, 12), date(2026, 7, 14)),
            Period(date(2026, 7, 15), date(2026, 7, 16)),
        )
        self.source = ChartSource(
            article="Přerušovaný půst",
            project="cs.wikipedia.org",
            url="https://example.invalid/synthetic-pageviews",
            fetched_at="2026-09-25T08:00:00+00:00",
            response_sha256="a" * 64,
        )

    def test_creates_headless_png_with_units_and_source_metadata(self):
        from PIL import Image

        figure = build_figure(self.periods, self.source)
        self.addCleanup(figure.clear)
        axes = figure.axes[0]
        self.assertEqual(axes.get_xlabel(), "Date (UTC)")
        self.assertEqual(axes.get_ylabel(), "Pageviews per day")
        self.assertEqual(axes.get_yscale(), "linear")
        self.assertEqual(axes.get_ylim()[0], 0)
        self.assertGreaterEqual(axes.get_ylim()[1], 30)

        png = render_png(self.periods, self.source)

        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertGreaterEqual(image.width, 1200)
            self.assertGreaterEqual(image.height, 800)
            self.assertEqual(image.info["Title"], self.source.article)
            self.assertEqual(image.info["Source"], self.source.url)
            details = json.loads(image.info["Description"])
            self.assertEqual(details["response_sha256"], self.source.response_sha256)
            self.assertIsNone(details["smoothing"])
            coverage = details["periods"]["baseline"]["coverage"]
            self.assertEqual(coverage["missing_days"], 1)

    def test_actual_plot_breaks_at_missing_dates_and_keeps_zero_markers(self):
        from matplotlib.path import Path as PlotPath

        figure = build_figure(self.periods, self.source)
        self.addCleanup(figure.clear)
        lines = {line.get_gid(): line for line in figure.axes[0].lines}
        baseline = lines["baseline-views"]
        self.assertTrue(math.isnan(baseline.get_ydata()[1]))
        self.assertEqual(baseline.get_ydata()[2], 0)
        self.assertEqual(baseline.get_marker(), "o")
        codes = [code for _, code in baseline.get_path().iter_segments()]
        self.assertEqual(codes.count(PlotPath.MOVETO), 2)
        self.assertNotIn(PlotPath.LINETO, codes)
        self.assertGreater(lines["baseline-missing"].get_ydata()[0], 1)
        self.assertNotEqual(baseline.get_color(), lines["current-views"].get_color())

    def test_all_missing_series_has_a_message_not_a_zero_line(self):
        periods = prepare_chart_data(
            make_series([None, None]),
            Period(date(2026, 1, 1), date(2026, 1, 1)),
            Period(date(2026, 1, 2), date(2026, 1, 2)),
        )
        figure = build_figure(periods, self.source)
        self.addCleanup(figure.clear)
        axes = figure.axes[0]
        self.assertTrue(any("No observations" in text.get_text() for text in axes.texts))
        for line in axes.lines:
            if line.get_gid().endswith("-views"):
                self.assertTrue(all(math.isnan(value) for value in line.get_ydata()))
        self.assertTrue(render_png(periods, self.source).startswith(b"\x89PNG"))

    def test_all_zero_series_still_shows_observations(self):
        periods = prepare_chart_data(
            make_series([0, 0]),
            Period(date(2026, 1, 1), date(2026, 1, 1)),
            Period(date(2026, 1, 2), date(2026, 1, 2)),
        )
        figure = build_figure(periods, self.source)
        self.addCleanup(figure.clear)
        axes = figure.axes[0]
        self.assertFalse(any("No observations" in text.get_text() for text in axes.texts))
        self.assertEqual(axes.get_ylim(), (0, 1))
        self.assertTrue(all(line.get_ydata()[0] == 0 for line in axes.lines))

    def test_equal_averages_with_different_shapes_produce_different_charts(self):
        baseline = Period(date(2026, 1, 1), date(2026, 3, 1))
        current = Period(date(2026, 3, 2), date(2026, 4, 30))
        gradual = [101 + 2 * (index // 3) for index in range(60)]
        spike = [100] * 59 + [1300]
        first = prepare_chart_data(make_series([100] * 60 + gradual), baseline, current)
        second = prepare_chart_data(make_series([100] * 60 + spike), baseline, current)
        self.assertEqual(first[1].summary.mean_daily_views_observed, 120)
        self.assertEqual(second[1].summary.mean_daily_views_observed, 120)
        self.assertEqual(first[1].values[-1], 139)
        self.assertEqual(second[1].values[-1], 1300)
        self.assertNotEqual(render_png(first, self.source), render_png(second, self.source))

    def test_text_and_utc_do_not_depend_on_user_matplotlib_settings(self):
        import matplotlib
        from matplotlib.dates import date2num

        source = replace(self.source, article=r"Literal $not_a_formula$ and \alpha")
        with matplotlib.rc_context({"timezone": "America/New_York", "text.usetex": True}):
            figure = build_figure(self.periods, source)
            self.addCleanup(figure.clear)
            formatter = figure.axes[0].xaxis.get_major_formatter()
            self.assertEqual(formatter(date2num(date(2026, 7, 12))), "2026-07-12")
            title = next(text for text in figure.texts if "Literal" in text.get_text())
            self.assertEqual(title.get_text(), source.article)
            self.assertFalse(title.get_parse_math())
            self.assertFalse(title.get_usetex())
            self.assertEqual(matplotlib.rcParams["timezone"], "America/New_York")
            self.assertTrue(matplotlib.rcParams["text.usetex"])


class PngStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.png = b"\x89PNG\r\n\x1a\nsynthetic storage test"

    def test_creates_parent_and_preserves_exact_bytes(self):
        path = self.root / "charts" / "test.png"
        self.assertEqual(save_png(self.png, path), path)
        self.assertEqual(path.read_bytes(), self.png)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_existing_file_is_never_overwritten(self):
        path = self.root / "test.png"
        path.write_bytes(b"original")
        with self.assertRaises(PageviewsError) as caught:
            save_png(self.png, path)
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(path.read_bytes(), b"original")

    def test_non_png_extension_does_not_create_output_directory(self):
        path = self.root / "charts" / "test.json"
        with self.assertRaises(PageviewsError) as caught:
            save_png(self.png, path)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertFalse(path.parent.exists())

    def test_dangling_symlink_is_not_followed(self):
        path = self.root / "link.png"
        target = self.root / "target.png"
        path.symlink_to(target)
        with self.assertRaises(PageviewsError) as caught:
            save_png(self.png, path)
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertFalse(target.exists())
        self.assertTrue(path.is_symlink())

    def test_failed_publish_leaves_no_partial_png(self):
        path = self.root / "test.png"
        with patch("tools.pageviews.charts.os.link", side_effect=PermissionError("test")):
            with self.assertRaises(PageviewsError) as caught:
                save_png(self.png, path)
        self.assertEqual(caught.exception.code, "chart_write_error")
        self.assertEqual(list(self.root.iterdir()), [])