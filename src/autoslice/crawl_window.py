"""The one time window of the circle-knowledge crawlers: nine months back and six months ahead of a day.

the channel's request; confirmed for the news crawler (「保持原来的-9到+6吧」).  The
timely-term crawler, the news crawler and the lane's graph reader all take it from here."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

LOOKBACK_MONTHS = 9
LOOKAHEAD_MONTHS = 6


def shift_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last_day))


@dataclass(frozen=True)
class CrawlWindow:
    as_of: dt.date
    start: dt.date
    end: dt.date

    @classmethod
    def around(
        cls,
        as_of: dt.date,
        *,
        lookback_months: int = LOOKBACK_MONTHS,
        lookahead_months: int = LOOKAHEAD_MONTHS,
    ) -> "CrawlWindow":
        if not 0 <= lookback_months <= 24 or not 0 <= lookahead_months <= 24:
            raise ValueError("lookback/lookahead months must be between 0 and 24")
        return cls(
            as_of=as_of,
            start=shift_months(as_of, -lookback_months),
            end=shift_months(as_of, lookahead_months),
        )

    def holds(self, valid_from: str | None, valid_until: str | None) -> bool:
        """Whether something current from `valid_from` to `valid_until` (ISO dates; None = open end) overlaps the
        window.  Nothing dated at all is not placed in time, so it is not held."""
        if not valid_from and not valid_until:
            return False
        starts = dt.date.fromisoformat(valid_from) if valid_from else None
        ends = dt.date.fromisoformat(valid_until) if valid_until else None
        return (starts is None or starts <= self.end) and (ends is None or ends >= self.start)
