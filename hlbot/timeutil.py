"""Clock access behind one seam so tests can control time."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime


class Clock:
    """Wall clock (UTC) and monotonic clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def monotonic(self) -> float:
        return time.monotonic()

    def iso(self) -> str:
        return self.now().isoformat(timespec="milliseconds")

    def utc_date(self) -> str:
        return self.now().strftime("%Y-%m-%d")


class ManualClock(Clock):
    """Test clock: wall time is set explicitly; monotonic time is real
    unless offset with ``advance``."""

    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)
        self._mono_offset = 0.0

    def now(self) -> datetime:
        return self._now

    def now_ms(self) -> int:
        return int(self._now.timestamp() * 1000)

    def monotonic(self) -> float:
        return time.monotonic() + self._mono_offset

    def set(self, value: datetime) -> None:
        self._now = value if value.tzinfo else value.replace(tzinfo=UTC)

    def advance(self, seconds: float, *, monotonic: bool = True) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
        if monotonic:
            self._mono_offset += seconds


def day_start_ms(utc_date: str) -> int:
    return int(datetime.strptime(utc_date, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


async def wait_event(event: asyncio.Event, seconds: float | None) -> bool:
    """Wait for ``event`` for at most ``seconds`` (``None``: forever).
    Returns whether it was set.

    Built on ``asyncio.timeout()`` rather than ``asyncio.wait_for()``:
    before Python 3.12, ``wait_for`` could swallow a cancellation that
    raced with its own timeout. A loop written as "wait, then go round
    again" would then keep running after being cancelled, and shutdown
    would wait for it forever."""
    if seconds is None:
        await event.wait()
        return True
    try:
        async with asyncio.timeout(seconds):
            await event.wait()
        return True
    except TimeoutError:
        return False
