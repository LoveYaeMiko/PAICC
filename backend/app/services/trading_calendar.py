"""A-share trading-day calendar (weekday + exchange holiday overrides).

The in-process scheduler used to gate every job on ``now.weekday() < 5``, so a
holiday still launched the live trader, the preclose order layer and the daily
close loop. A real calendar is needed; because the exchange announces holidays
only for the current year, this module ships:

* the **observed** 2026 closures, derived from the HS300 benchmark calendar
  (``data/benchmark/hs300_index.csv``, i.e. weekdays with no trading) — these are
  facts, not guesses;
* a settings override ``quant_holidays`` (comma-separated ISO dates) that an
  operator appends from the exchange notice for the rest of the year, e.g. the
  Mid-Autumn / National-Day block (see the SSE notice
  https://www.sse.com.cn/disclosure/announcement/general/ for the current year).

When the list is empty the behaviour degrades to weekday-only, exactly as
before — the module never guesses a date.
"""
from __future__ import annotations

from datetime import date, datetime

#: 2026 weekdays with no trading, derived from the benchmark calendar through
#: 2026-09-07 (New Year, Spring Festival, Qingming, Labour Day, Dragon Boat).
OBSERVED_2026_HOLIDAYS: frozenset[str] = frozenset(
    {
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
    }
)


def _coerce(d: date | datetime | str | None) -> date:
    if d is None:
        return datetime.now().date()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.fromisoformat(str(d)[:10]).date()


def holiday_dates(extra: str | None = None) -> set[str]:
    """Observed holidays plus the operator-supplied ``extra`` ISO dates."""
    out = set(OBSERVED_2026_HOLIDAYS)
    if extra:
        for item in str(extra).replace(";", ",").split(","):
            token = item.strip()
            if not token:
                continue
            try:
                out.add(_coerce(token).isoformat())
            except ValueError:
                continue
    return out


def is_trading_day(d: date | datetime | str | None = None, extra: str | None = None) -> bool:
    """True when ``d`` is a weekday that is not an exchange holiday."""
    day = _coerce(d)
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in holiday_dates(extra)


__all__ = ["OBSERVED_2026_HOLIDAYS", "holiday_dates", "is_trading_day"]
