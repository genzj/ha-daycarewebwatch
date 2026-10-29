"""Tests for the viewing window."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.daycarewebwatch.schedule import ViewingWindow

TZ = ZoneInfo("America/Toronto")
WINDOW = ViewingWindow.from_options("06:30:00", "18:30:00", ["mon", "tue", "wed", "thu", "fri"])
IN_WIN = timedelta(minutes=10)
MAX = timedelta(hours=3)


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


def test_from_options_accepts_time_objects() -> None:
    w = ViewingWindow.from_options(time(7), time(8), ["sat", "bogus"])
    assert w.days == frozenset({5})


def test_contains() -> None:
    assert WINDOW.contains(dt(2026, 10, 28, 9, 0))  # Wednesday
    assert not WINDOW.contains(dt(2026, 10, 28, 18, 30))
    assert not WINDOW.contains(dt(2026, 10, 31, 9, 0))  # Saturday


def test_next_start() -> None:
    assert WINDOW.next_start(dt(2026, 10, 28, 5, 0)) == dt(2026, 10, 28, 6, 30)
    assert WINDOW.next_start(dt(2026, 10, 28, 20, 0)) == dt(2026, 10, 29, 6, 30)
    # Friday evening -> Monday morning
    assert WINDOW.next_start(dt(2026, 10, 30, 19, 0)) == dt(2026, 11, 2, 6, 30)
    assert ViewingWindow(time(6), time(7), frozenset()).next_start(dt(2026, 1, 1, 1)) is None
    assert ViewingWindow(time(8), time(7), frozenset({0})).next_start(dt(2026, 1, 1, 1)) is None


def test_closed_interval() -> None:
    assert WINDOW.closed_interval(dt(2026, 10, 28, 9, 0), IN_WIN, MAX) == IN_WIN
    # One hour before opening: wake exactly at opening.
    assert WINDOW.closed_interval(dt(2026, 10, 28, 5, 30), IN_WIN, MAX) == timedelta(hours=1)
    # Weekend: capped.
    assert WINDOW.closed_interval(dt(2026, 10, 31, 12, 0), IN_WIN, MAX) == MAX
    # Seconds before opening: never below one minute.
    assert WINDOW.closed_interval(dt(2026, 10, 28, 6, 29, 59), IN_WIN, MAX) == timedelta(minutes=1)
    empty = ViewingWindow(time(6), time(7), frozenset())
    assert empty.closed_interval(dt(2026, 10, 28, 9, 0), IN_WIN, MAX) == MAX


def test_closed_interval_across_dst() -> None:
    # 2026-11-01 02:00 EDT -> 01:00 EST (Sunday). Saturday 23:00 -> Monday 06:30 is capped anyway,
    # so use a window that opens Sunday 04:00 to observe the extra hour.
    w = ViewingWindow(time(4), time(5), frozenset({6}))
    assert w.closed_interval(dt(2026, 11, 1, 0, 0), IN_WIN, timedelta(hours=10)) == timedelta(
        hours=5
    )
