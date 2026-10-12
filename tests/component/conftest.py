"""Component-test harness: the full application (engine, reconciler,
signal manager, health, queue, notifier, database) wired to the
scriptable simulated exchange. No network."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from typing import Any, Optional

import pytest

from hlbot.config import Settings
from hlbot.core.app import BotApp
from hlbot.domain.risk import RiskConfig
from hlbot.exchange.sim import SimExchange
from hlbot.redact import clear_secrets
from hlbot.timeutil import ManualClock

COIN = "xyz:GOLD"
CHANNEL = -1004307627275
START = datetime(2026, 9, 14, 10, 1, 0, tzinfo=UTC)

SELL_TEXT = (
    "#GOLD SELL NOW\nGold sell Zone 4286- 4289\n\nSL : 4296\n\n"
    "TP1 : 4283\nTP2 : 4280\nTP3 : 4277\nTP4 : 4274\nTP5 : 4270\n\n"
    "or set your stop loss according to your margin\n#Vip_Signals"
)
BUY_TEXT = (
    "#GOLD BUY NOW\nGold buy Zone 4310- 4313\n\nSL : 4303\n\n"
    "TP1 : 4316\nTP2 : 4319\nTP3 : 4322\nTP4 : 4325\nTP5 : 4329\n#Vip_Signals"
)


def make_settings(tmp_path, **overrides: Any) -> Settings:
    risk = overrides.pop("risk", None) or RiskConfig()
    risk_over = {k[5:]: overrides.pop(k) for k in list(overrides) if k.startswith("risk_")}
    if risk_over:
        risk = dataclasses.replace(risk, **risk_over)
    base: dict[str, Any] = dict(
        env="paper",
        data_dir=str(tmp_path / "data"),
        log_dir=str(tmp_path / "logs"),
        telegram_enabled=False,
        api_enabled=False,
        notify_enabled=False,
        paper_start_equity=1000.0,
        entry_retry_min_s=0.0,
        protect_soft_ms=150,
        protect_hard_ms=450,
        flatten_deadline_ms=500,
        reconcile_interval_s=0.15,
        risk=risk,
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    """One bot process plus the exchange it talks to."""

    def __init__(self, tmp_path, sim: Optional[SimExchange] = None, clock: Optional[ManualClock] = None, **overrides: Any):
        self.tmp_path = tmp_path
        self.overrides = overrides
        self.clock = clock or ManualClock(START)
        self.sim = sim or SimExchange(equity=overrides.get("paper_start_equity", 1000.0), clock=self.clock)
        if not self.sim.instruments:
            self.sim.add_instrument(COIN, sz_decimals=4, max_leverage=25)
        self.sent: list[str] = []
        self.send_ok = True
        self.app: BotApp = None  # type: ignore[assignment]
        self._next_message = 1477

    async def _sender(self, text: str) -> None:
        if not self.send_ok:
            raise RuntimeError("bot api unreachable")
        self.sent.append(text)

    async def start(self) -> Harness:
        self.settings = make_settings(self.tmp_path, **self.overrides)
        self.app = BotApp(
            self.settings, adapter=self.sim, clock=self.clock, sender=self._sender,
            fast_poll_s=0.02, watchdog_s=0.05, market_fresh_for_s=0.2, install_signal_handlers=False,
        )
        await self.app.start()
        self.app.queue._scale = 0.01  # shrink rate-limit waits
        return self

    # ---- shortcuts
    @property
    def db(self):
        return self.app.db

    @property
    def engine(self):
        return self.app.engine

    @property
    def health(self):
        return self.app.health

    async def quote(self, bid: float, ask: Optional[float] = None, mark: Optional[float] = None, oracle: Optional[float] = None) -> None:
        await self.sim.set_quote(COIN, bid, ask if ask is not None else round(bid + 0.1, 1), mark, oracle)
        await self.settle()

    def post(self, text: str = SELL_TEXT, *, channel: int = CHANNEL, message_id: Optional[int] = None, ref: str = "@JugnoTrad_Hub84",
             date: Optional[str] = None) -> Optional[int]:
        if message_id is None:
            message_id = self._next_message
            self._next_message += 1
        return self.app.ingest_message(
            {"channel_id": channel, "message_id": message_id, "channel_ref": ref,
             "date": date or self.clock.now().isoformat(), "text": text, "has_media": False, "reply_to": None},
            "test",
        )

    async def settle(self, rounds: int = 6) -> None:
        """Let queued work finish: entry evaluations, stream events, trade loops."""
        for _ in range(rounds):
            await self.app.signals.idle()
            await self.sim.drain()
            await asyncio.sleep(0.03)

    async def until(self, predicate, timeout: float = 4.0, message: str = "condition") -> None:  # noqa: ASYNC109
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await self.sim.drain()
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {message}")

    def signal(self, signal_id: int) -> dict[str, Any]:
        return self.db.get("signals", signal_id)

    def trades(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM trades ORDER BY id")

    def trade(self, trade_id: Optional[int] = None) -> dict[str, Any]:
        if trade_id is None:
            return self.trades()[-1]
        return self.db.get("trades", trade_id)

    def orders(self, role: Optional[str] = None, trade_id: Optional[int] = None) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM orders ORDER BY id")
        return [r for r in rows if (role is None or r["role"] == role) and (trade_id is None or r["trade_id"] == trade_id)]

    def audit(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        if kind:
            return self.db.all("SELECT * FROM audit_events WHERE kind = ? ORDER BY seq", (kind,))
        return self.db.all("SELECT * FROM audit_events ORDER BY seq")

    def alerts(self) -> list[str]:
        return [r["text"] for r in self.db.all("SELECT text FROM outbox WHERE kind = 'alert' ORDER BY id")]

    def entry_requests(self) -> int:
        """Entry orders the exchange actually received."""
        entry_cloids = {o["cloid"] for o in self.orders("ENTRY")}
        return sum(1 for r in self.sim.requests if r["kind"] == "order" and entry_cloids & set(r["detail"]))

    async def open_protected_trade(self, text: str = SELL_TEXT, bid: float = 4287.0) -> dict[str, Any]:
        await self.quote(bid)
        self.post(text)
        await self.settle()
        await self.until(lambda: self.trades() and self.trade()["state"] == "PROTECTED", message="trade PROTECTED")
        return self.trade()

    def freeze_background(self) -> None:
        """Crash-injection aid: once the "process" has died at the chosen
        point, nothing else in it may keep working. Call before the
        fatal step so the reconciler cannot heal the state the test
        wants the *next* process to find."""

        async def nothing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "frozen": True}

        self.app.reconciler.run_once = nothing  # type: ignore[method-assign]
        self.app.signals.tick = nothing  # type: ignore[method-assign]

    async def crash(self) -> None:
        """Kill the process without any shutdown logic: no notifications,
        no cancels, tasks simply cease to exist."""
        app = self.app
        tasks = list(app.tasks) + list(app.engine._loops.values()) + list(app.signals._tasks.values())
        if app.queue._task:
            tasks.append(app.queue._task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        app.db.close()
        app.lock.release()
        self.sim._handlers = type(self.sim._handlers)()  # the dead process no longer receives stream events

    async def restart(self) -> Harness:
        """A fresh process on the same database, against the same exchange."""
        fresh = Harness(self.tmp_path, sim=self.sim, clock=self.clock, **self.overrides)
        fresh._next_message = self._next_message
        return await fresh.start()

    async def stop(self) -> None:
        if self.app is not None and self.app.started:
            await self.app.shutdown()


@pytest.fixture
async def make(tmp_path):
    """Factory fixture: ``h = await make(**setting_overrides)``."""
    created: list[Harness] = []

    async def factory(start: bool = True, **overrides: Any) -> Harness:
        harness = Harness(tmp_path, **overrides)
        created.append(harness)
        if start:
            await harness.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
            await harness.start()
        return harness

    yield factory
    for harness in created:
        try:
            await harness.stop()
        except Exception:
            pass
    clear_secrets()


@pytest.fixture
async def h(make):
    return await make()
