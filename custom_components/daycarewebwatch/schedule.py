"""Viewing-window math used to slow down polling at night and on weekends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from .const import WEEKDAYS


@dataclass(frozen=True, slots=True)
class ViewingWindow:
    """Local-time window in which the daycare is expected to be open."""

    start: time
    end: time
    days: frozenset[int]  # datetime.weekday() values

    @classmethod
    def from_options(cls, start: str | time, end: str | time, days: list[str]) -> ViewingWindow:
        """Build from config entry options."""
        return cls(
            start=start if isinstance(start, time) else time.fromisoformat(start),
            end=end if isinstance(end, time) else time.fromisoformat(end),
            days=frozenset(WEEKDAYS.index(day) for day in days if day in WEEKDAYS),
        )

    def contains(self, now: datetime) -> bool:
        """Return True if now (aware, local) is inside the window."""
        return now.weekday() in self.days and self.start <= now.time() < self.end

    def next_start(self, now: datetime) -> datetime | None:
        """Return the next window start strictly after now, or None if no days."""
        if not self.days or self.start >= self.end:
            return None
        for offset in range(8):
            day = (now + timedelta(days=offset)).date()
            if day.weekday() not in self.days:
                continue
            candidate = datetime.combine(day, self.start, tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
        return None

    def closed_interval(self, now: datetime, in_window: timedelta, maximum: timedelta) -> timedelta:
        """Return the polling interval while viewing is closed."""
        if self.contains(now):
            return in_window
        if (start := self.next_start(now)) is None:
            return maximum
        # Subtract in UTC so DST transitions are accounted for.
        delta = start.astimezone(UTC) - now.astimezone(UTC)
        return max(min(delta, maximum), timedelta(minutes=1))
