from datetime import date, timedelta

from tools.pageviews.models import DailyViews
from tools.pageviews.validation import ValidatedSeries


def calendar_series(start, end, value_for_day):
    return ValidatedSeries(days=tuple(
        DailyViews(day, value_for_day(day, offset))
        for offset in range((end - start).days + 1)
        for day in (start + timedelta(days=offset),)
    ))


def known_trend_series(*, slope_per_day=1, residuals=True):
    start, end = date(2024, 1, 1), date(2025, 12, 31)
    perturbations = {0: 1, 7: -1, 14: -1, 21: 1} if residuals else {}
    return calendar_series(start, end, lambda day, offset: (
        2000 + slope_per_day * offset + 10 * day.month + 3 * day.weekday()
        + perturbations.get(offset, 0)
    ))