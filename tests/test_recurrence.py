from datetime import date, datetime

from schulblick.web.family import FamilyEvent, occurrence_dates


def ev(start, repeat, until=None, end=None, exdates=(), skip=False):
    return FamilyEvent(id=1, title="t", start=start, end=end, all_day=not isinstance(start, datetime), location="",
                       note="", audience=["*"], created_by=1, created_by_name="Papa", created_at="", updated_at="",
                       repeat=repeat, repeat_until=until, exdates=list(exdates), skip_holidays=skip)


def test_weekly_and_biweekly():
    e = ev(datetime(2026, 10, 1, 17, 0), "weekly")  # Thursday
    assert occurrence_dates(e, date(2026, 10, 19), date(2026, 11, 8)) == [
        date(2026, 10, 22), date(2026, 10, 29), date(2026, 11, 5)]
    b = ev(date(2026, 10, 1), "biweekly")
    assert occurrence_dates(b, date(2026, 10, 1), date(2026, 11, 1)) == [
        date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 29)]


def test_until_exdates_and_daily():
    e = ev(date(2026, 10, 19), "daily", until=date(2026, 10, 23), exdates=["2026-10-21"])
    assert occurrence_dates(e, date(2026, 10, 1), date(2026, 12, 1)) == [
        date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 22), date(2026, 10, 23)]


def test_monthly_skips_missing_days_and_yearly():
    m = ev(date(2026, 1, 31), "monthly")
    got = occurrence_dates(m, date(2026, 1, 1), date(2026, 6, 30))
    assert got == [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)]
    y = ev(date(2015, 10, 23), "yearly")
    assert occurrence_dates(y, date(2026, 1, 1), date(2027, 12, 31)) == [date(2026, 10, 23), date(2027, 10, 23)]


def test_multi_day_occurrence_overlapping_window_start():
    e = ev(date(2026, 10, 2), "weekly", end=date(2026, 10, 4))  # Fri-Sun every week
    assert occurrence_dates(e, date(2026, 10, 18), date(2026, 10, 20)) == [date(2026, 10, 16)]


def test_skip_holidays():
    e = ev(date(2026, 12, 1), "weekly", skip=True)
    no_school = {date(2026, 12, 29), date(2027, 1, 5)}
    assert occurrence_dates(e, date(2026, 12, 20), date(2027, 1, 13), no_school) == [
        date(2026, 12, 22), date(2027, 1, 12)]
