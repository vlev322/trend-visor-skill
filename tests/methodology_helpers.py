from datetime import timedelta

from tools.pageviews.models import DailyViews
from tools.pageviews.validation import ValidatedSeries


def calendar_series(start, end, value_for_day):
    return ValidatedSeries(days=tuple(
        DailyViews(day, value_for_day(day, offset))
        for offset in range((end - start).days + 1)
        for day in (start + timedelta(days=offset),)
    ))
