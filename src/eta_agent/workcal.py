from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


class CalendarCoverageError(ValueError):
    pass


@dataclass(frozen=True)
class WorkCalendar:
    name: str
    from_date: date
    to_date: date
    holidays: frozenset[date]

    def _check(self, d: date) -> None:
        if not (self.from_date <= d <= self.to_date):
            raise CalendarCoverageError(f"{d} 不在工作日曆 {self.name}（{self.from_date}~{self.to_date}）範圍內")

    def is_workday(self, d: date) -> bool:
        self._check(d)
        return d not in self.holidays

    def next_workday_on_or_after(self, d: date) -> date:
        while not self.is_workday(d):
            d += timedelta(days=1)
        return d

    def add_workdays(self, d: date, n: int) -> date:
        if n < 0:
            raise ValueError("n 必須 >= 0")
        d = self.next_workday_on_or_after(d)
        while n > 0:
            d += timedelta(days=1)
            if self.is_workday(d):
                n -= 1
        return d

    def workdays_between(self, start: date, end: date) -> int:
        if end <= start:
            return 0
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_workday(d):
                n += 1
        return n


def weekend_calendar(name: str, year_from: int, year_to: int, extra: set[date] | None = None) -> WorkCalendar:
    start, end = date(year_from, 1, 1), date(year_to, 12, 31)
    hol = set(extra or set())
    d = start
    while d <= end:
        if d.weekday() >= 5:
            hol.add(d)
        d += timedelta(days=1)
    return WorkCalendar(name, start, end, frozenset(hol))


TW_2026_HOLIDAYS = {
    date(2026, 1, 1),
    *(date(2026, 2, d) for d in (16, 17, 18, 19, 20)),
    date(2026, 2, 27),
    date(2026, 4, 3), date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 6, 19),
    date(2026, 9, 25), date(2026, 9, 28),
    date(2026, 10, 9), date(2026, 10, 26),
    date(2026, 12, 25),
}


def tw_2026() -> WorkCalendar:
    return weekend_calendar("TW 2026", 2026, 2026, TW_2026_HOLIDAYS)
