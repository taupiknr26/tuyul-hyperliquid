"""Single-writer action queue (PRD §9.6.2, §9.6.3, §11.2).

The only path to the exchange for signed actions. One worker, so nonces
are issued in order from one place: ``nonce = max(now_ms, last + 1)``.
Work is ordered by priority: protect > close > cancel > entry.

Rate limits: a throttled *entry* is refused (the signal simply keeps
waiting); a throttled protective action is retried in place with
jittered exponential backoff, because a 429 means the request was not
processed. While throttled, the queue spaces actions 10 s apart and the
priority order decides which one goes next: place/repair SL, then
close, then cancel.
"""

from __future__ import annotations

import asyncio
import enum
import itertools
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from hlbot.core.health import BLOCK, Health
from hlbot.core.metrics import Metrics
from hlbot.db.database import Database
from hlbot.exchange.base import ErrorClass, ExchangeError
from hlbot.timeutil import Clock

logger = logging.getLogger("hlbot.queue")

BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 30.0
THROTTLED_SPACING_S = 10.0
THROTTLE_CLEAR_S = 60.0
MAX_THROTTLE_RETRIES = 8


class Priority(enum.IntEnum):
    PROTECT = 0
    CLOSE = 1
    CANCEL = 2
    ENTRY = 3


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    label: str = field(compare=False)
    fn: Callable[[int], Awaitable[Any]] = field(compare=False)
    future: asyncio.Future[Any] = field(compare=False)
    audit: dict[str, Any] = field(compare=False, default_factory=dict)
    refs: dict[str, Optional[int]] = field(compare=False, default_factory=dict)
    retries: int = field(compare=False, default=0)


class ActionQueue:
    def __init__(
        self,
        db: Database,
        health: Health,
        clock: Optional[Clock] = None,
        metrics: Optional[Metrics] = None,
        *,
        backoff_scale: float = 1.0,
    ):
        self.db = db
        self.health = health
        self.clock = clock or Clock()
        self.metrics = metrics or Metrics()
        self._queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._last_nonce = int(db.kv_get("last_nonce", 0) or 0)
        self._throttled_until = 0.0
        self._last_action_at = 0.0
        self._backoff = BACKOFF_START_S
        self._scale = backoff_scale  # tests shrink the waits
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ nonce
    def next_nonce(self) -> int:
        nonce = max(self.clock.now_ms(), self._last_nonce + 1)
        self._last_nonce = nonce
        self.db.kv_set("last_nonce", nonce)
        return nonce

    @property
    def throttled(self) -> bool:
        return self.clock.monotonic() < self._throttled_until

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="action-queue")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if not item.future.done():
                item.future.set_exception(ExchangeError(ErrorClass.OTHER, "action queue stopped"))

    # ------------------------------------------------------------ submit
    async def submit(
        self,
        priority: Priority,
        label: str,
        fn: Callable[[int], Awaitable[Any]],
        *,
        audit: Optional[dict[str, Any]] = None,
        trade_id: Optional[int] = None,
        order_id: Optional[int] = None,
    ) -> Any:
        """Run ``fn(nonce)`` on the worker and return its result. Raises
        whatever ``fn`` raises. An entry submitted while the account is
        rate-limited is refused immediately."""
        if priority == Priority.ENTRY and self.throttled:
            raise ExchangeError(ErrorClass.RATE_LIMIT, "entries are blocked while rate-limited")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(
            _Item(int(priority), next(self._seq), label, fn, future, audit or {}, {"trade_id": trade_id, "order_id": order_id})
        )
        self.metrics.gauge("action_queue_depth", self._queue.qsize())
        return await future

    # ------------------------------------------------------------ worker
    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item.future.done():
                continue
            if self.throttled:
                if item.priority == Priority.ENTRY:
                    item.future.set_exception(ExchangeError(ErrorClass.RATE_LIMIT, "entries are blocked while rate-limited"))
                    continue
                wait = THROTTLED_SPACING_S * self._scale - (self.clock.monotonic() - self._last_action_at)
                if wait > 0:
                    await asyncio.sleep(wait)
            await self._execute(item)
            self.metrics.gauge("action_queue_depth", self._queue.qsize())

    async def _execute(self, item: _Item) -> None:
        nonce = self.next_nonce()
        refs = item.refs
        self.db.audit(
            "exchange.request",
            {"action": item.label, "nonce": nonce, "priority": Priority(item.priority).name, **item.audit},
            trade_id=refs.get("trade_id"),
            order_id=refs.get("order_id"),
        )
        self._last_action_at = self.clock.monotonic()
        try:
            result = await item.fn(nonce)
        except ExchangeError as exc:
            self.db.audit(
                "exchange.response",
                {"action": item.label, "nonce": nonce, "error": exc.kind.value, "text": exc.text},
                trade_id=refs.get("trade_id"),
                order_id=refs.get("order_id"),
            )
            if exc.kind == ErrorClass.RATE_LIMIT:
                self._on_rate_limit(item, exc)
                return
            self.metrics.inc("exchange_action_errors")
            if not item.future.done():
                item.future.set_exception(exc)
            return
        except asyncio.CancelledError:
            if not item.future.done():
                item.future.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            self.db.audit(
                "exchange.response",
                {"action": item.label, "nonce": nonce, "error": "exception", "text": type(exc).__name__},
                trade_id=refs.get("trade_id"),
                order_id=refs.get("order_id"),
            )
            if not item.future.done():
                item.future.set_exception(exc)
            return
        self.db.audit(
            "exchange.response",
            {"action": item.label, "nonce": nonce, "result": _summarise(result)},
            trade_id=refs.get("trade_id"),
            order_id=refs.get("order_id"),
        )
        self.metrics.inc("exchange_actions")
        if self._throttled_until and not self.throttled:
            self._throttled_until = 0.0
            self._backoff = BACKOFF_START_S
            self.health.clear_condition("rate_limit")
        if not item.future.done():
            item.future.set_result(result)

    def _on_rate_limit(self, item: _Item, exc: ExchangeError) -> None:
        self.metrics.inc("rate_limit_hits")
        self._throttled_until = self.clock.monotonic() + THROTTLE_CLEAR_S * self._scale
        self.health.set_condition("rate_limit", "exchange rate limit reached", level=BLOCK, auto_resume=True, alert=True)
        if item.priority == Priority.ENTRY or item.retries >= MAX_THROTTLE_RETRIES:
            if not item.future.done():
                item.future.set_exception(exc)
            return
        item.retries += 1
        delay = self._backoff * (0.5 + random.random() * 0.5) * self._scale
        self._backoff = min(BACKOFF_CAP_S, self._backoff * 2.0)
        logger.warning("rate limited; retrying %s in %.1fs", item.label, delay)

        async def requeue() -> None:
            await asyncio.sleep(delay)
            self._queue.put_nowait(item)

        asyncio.get_running_loop().create_task(requeue())

    def clear_throttle(self) -> None:
        """Called when the periodic headroom check shows the limit has lifted."""
        if self._throttled_until:
            self._throttled_until = 0.0
            self._backoff = BACKOFF_START_S
            self.health.clear_condition("rate_limit")


def _summarise(result: Any) -> Any:
    if isinstance(result, list):
        return [_summarise(r) for r in result]
    if hasattr(result, "status") and hasattr(result, "oid"):
        error = getattr(result, "error", None)
        return {
            "status": result.status,
            "oid": result.oid,
            "total_size": getattr(result, "total_size", None),
            "avg_price": getattr(result, "avg_price", None),
            "error": error.text if error else None,
        }
    if isinstance(result, ExchangeError):
        return {"error": result.text}
    return result if isinstance(result, (str, int, float, bool, type(None), dict)) else str(result)
