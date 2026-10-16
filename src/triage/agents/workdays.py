"""Working-day arithmetic for the reporting delay (F4).

RBI circular, after Table 2: working days are "counted as per the working
schedule of the home branch of the customer excluding the date of receiving
the communication". We count days d with from_date < d <= to_date that are
working days. Non-working days: Sundays, the 2nd and 4th Saturdays (RBI
schedule for banks), national holidays below, plus EXTRA_HOLIDAYS from config
(state/branch holidays). Branch-specific schedules are not available in the
synthetic data, which is stated in every result that uses this number.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

NATIONAL_HOLIDAYS_MMDD = {"01-26", "08-15", "10-02"}


def _second_or_fourth_saturday(d: date) -> bool:
    return d.weekday() == 5 and ((d.day - 1) // 7 + 1) in (2, 4)


def is_working_day(d: date, extra: set[str] | None = None) -> bool:
    if d.weekday() == 6 or _second_or_fourth_saturday(d):
        return False
    if d.strftime("%m-%d") in NATIONAL_HOLIDAYS_MMDD:
        return False
    if extra and d.isoformat() in extra:
        return False
    return True


def working_days_between(start: date | datetime, end: date | datetime, extra_holidays: str = "") -> tuple[int, int, list[str]]:
    """Return (working_days, calendar_days, excluded_non_working_dates)."""
    s = start.date() if isinstance(start, datetime) else start
    e = end.date() if isinstance(end, datetime) else end
    extra = {x.strip() for x in extra_holidays.split(",") if x.strip()}
    if e <= s:
        return 0, max(0, (e - s).days), []
    wd, excluded = 0, []
    d = s + timedelta(days=1)
    while d <= e:
        if is_working_day(d, extra):
            wd += 1
        else:
            excluded.append(d.isoformat())
        d += timedelta(days=1)
    return wd, (e - s).days, excluded
