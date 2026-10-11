"""Reconciler: continuous reconciliation, startup recovery and the
slow-moving health checks (FR-058 .. FR-060, FR-070; PRD §9.6.8, §10.7,
§12.5, §14).

The exchange is the source of truth for orders, fills, positions and
balances. Local state is the source of truth only for intent. A pass
fetches one consistent view over REST, applies fills idempotently,
drives every open trade against that view, and flags anything on a
managed asset that the bot did not create.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Optional

from hlbot.config import Settings
from hlbot.core.action_queue import ActionQueue, Priority
from hlbot.core.engine import Engine, ExchangeView
from hlbot.core.health import BLOCK, PAUSE, Health
from hlbot.core.metrics import Metrics
from hlbot.core.notifier import Notifier
from hlbot.db.database import Database
from hlbot.domain.planning import parse_iso
from hlbot.domain.risk import daily_loss_reached
from hlbot.domain.states import ORDER_TERMINAL, ORDER_UNRESOLVED, TRADE_TERMINAL
from hlbot.exchange.base import ExchangeAdapter, ExchangeError
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.reconciler")

INSTRUMENT_REFRESH_S = 300.0
LIMITS_REFRESH_S = 3600.0
UNREACHABLE_PAUSE_S = 30.0
CLOCK_DRIFT_PAUSE_MS = 2000
RATE_HEADROOM_ALERT = 0.20
KEY_EXPIRY_PAUSE_MS = 24 * 3600 * 1000
KEY_EXPIRY_WARN_MS = 14 * 24 * 3600 * 1000
SNAPSHOT_EVERY_S = 60.0


class StartupError(Exception):
    """Startup must fail closed (FR-070)."""


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        adapter: ExchangeAdapter,
        engine: Engine,
        health: Health,
        notifier: Notifier,
        queue: ActionQueue,
        clock: Optional[Clock] = None,
        metrics: Optional[Metrics] = None,
    ):
        self.s = settings
        self.db = db
        self.adapter = adapter
        self.engine = engine
        self.health = health
        self.notifier = notifier
        self.queue = queue
        self.clock = clock or Clock()
        self.metrics = metrics or Metrics()
        self._lock = asyncio.Lock()
        self._kick = asyncio.Event()
        self._drift: deque[int] = deque(maxlen=4)  # about one minute at the default cadence (§10.7)
        self._unreachable_since: Optional[float] = None
        self._last_instruments = 0.0
        self._last_limits = 0.0
        self._last_snapshot = 0.0
        self._last_positions: dict[str, float] = {}
        self.last_ok_at: Optional[str] = None
        self.last_view: Optional[ExchangeView] = None

    def kick(self) -> None:
        """Ask for a pass now (after a reconnect or an operator command)."""
        self._kick.set()

    # ================================================================== startup (PRD §9.6.8)
    async def startup(self, max_wait_s: float = 45.0) -> None:
        """Steps 3-8. Entries stay disabled until the recovery invariant
        holds: no unresolved order, no trade entering or unprotected."""
        self.health.clear_auto_conditions()
        self.health.set_condition("recovering", "startup recovery in progress", level=BLOCK, auto_resume=True, alert=False)
        await self.refresh_instruments(startup=True)
        await self.check_signer(startup=True)
        await self.check_limits()
        for coin in self.s.coins:
            try:
                await self.engine.on_quote(await self.adapter.get_book(coin))
                await self.engine.on_context(await self.adapter.get_market(coin))
            except ExchangeError as exc:
                logger.warning("initial market data for %s unavailable: %s", coin, exc)
        try:
            await self.engine.view()
        except ExchangeError as exc:
            raise StartupError(f"account state cannot be read: {exc}") from exc

        deadline = self.clock.monotonic() + max_wait_s
        while True:
            await self.run_once("startup")
            if self.recovery_invariant_ok():
                break
            if self.clock.monotonic() >= deadline:
                self.notifier.alert(
                    "Startup recovery incomplete", "Entries stay disabled until every order and trade is resolved.", critical=True
                )
                return
            await asyncio.sleep(min(0.5, self.engine.fast_poll_s))
        self.health.clear_condition("recovering")
        self.db.audit("recovery.complete", {"open_trades": len(self.engine.open_trades())})

    def recovery_invariant_ok(self) -> bool:
        """PRD §14 "Recovery invariant"."""
        marks = ", ".join("?" for _ in ORDER_UNRESOLVED)
        unresolved = self.db.scalar(f"SELECT COUNT(*) FROM orders WHERE state IN ({marks})", tuple(ORDER_UNRESOLVED), default=0)
        exposed = self.db.scalar("SELECT COUNT(*) FROM trades WHERE state IN ('ENTERING', 'OPEN_UNPROTECTED')", default=0)
        return not unresolved and not exposed

    # ================================================================== periodic checks
    async def refresh_instruments(self, startup: bool = False) -> None:
        """FR-021: resolve asset ids and specs from the exchange at
        startup and at least every 5 minutes; block delisted assets and
        assets at their open-interest cap."""
        try:
            instruments = await self.adapter.get_instruments()
            blocked = set(await self.adapter.get_blocked_coins())
        except ExchangeError as exc:
            if startup:
                raise StartupError(f"instrument metadata cannot be read: {exc}") from exc
            logger.warning("instrument refresh failed: %s", exc)
            return
        self._last_instruments = self.clock.monotonic()
        managed = {c: instruments.get(c) for c in self.s.coins}
        if startup and not any(managed.values()):
            raise StartupError(f"none of the configured coins exist on the exchange: {', '.join(self.s.coins)}")
        self.engine.instruments = {c: i for c, i in managed.items() if i is not None}
        for coin, inst in managed.items():
            key = f"asset_blocked:{coin}"
            if inst is None:
                self.health.set_condition(key, f"{coin} is not listed", level=BLOCK, scope=coin, auto_resume=True, alert=True)
            elif inst.is_delisted:
                self.health.set_condition(key, f"{coin} is delisted", level=BLOCK, scope=coin, auto_resume=True, alert=True)
            elif coin in blocked:
                self.health.set_condition(key, f"{coin} is at its open-interest cap", level=BLOCK, scope=coin, auto_resume=True, alert=True)
            elif inst.only_isolated and self.s.margin_mode == "cross":
                self.health.set_condition(
                    key, f"{coin} allows isolated margin only: set HL_MARGIN_MODE=isolated", level=BLOCK, scope=coin, auto_resume=True, alert=True
                )
            else:
                self.health.clear_condition(key)
            if inst is not None and self.s.risk.leverage > inst.max_leverage:
                message = f"RISK_LEVERAGE {self.s.risk.leverage} exceeds the maximum {inst.max_leverage} for {coin}"
                if startup:
                    raise StartupError(message)

    async def check_signer(self, startup: bool = False) -> None:
        """FR-070 / SR-02: the signing key must be an authorised agent;
        track its expiry."""
        if self.s.is_paper:
            return
        try:
            info = await self.adapter.verify_signer()
        except ExchangeError as exc:
            if startup:
                raise StartupError(f"signer authorisation cannot be verified: {exc}") from exc
            logger.warning("signer check failed: %s", exc)
            return
        if not info.authorized:
            if startup:
                raise StartupError(f"the signing key is not authorised for the configured account: {info.detail}")
            self.health.set_condition("agent_key", f"signer no longer authorised: {info.detail}", level=PAUSE, auto_resume=False)
            return
        self.db.kv_set("signer", {"expires_at_ms": info.expires_at_ms, "checked_at": self.clock.iso()})
        if info.expires_at_ms is None:
            return
        left = info.expires_at_ms - self.clock.now_ms()
        if left <= KEY_EXPIRY_PAUSE_MS:
            self.health.set_condition(
                "agent_key", f"agent key expires in {left / 3600000:.1f} h: approve a NEW agent key (never reuse an address)",
                level=PAUSE, auto_resume=False,
            )
        elif left <= KEY_EXPIRY_WARN_MS:
            day = self.clock.utc_date()
            if self.db.kv_get("agent_expiry_warned") != day:
                self.db.kv_set("agent_expiry_warned", day)
                self.notifier.alert("Agent key expires soon", f"{left / 86400000:.1f} days left. Approve a new agent key.", critical=False)

    async def check_limits(self) -> None:
        """NFR-014: address-based headroom at startup and hourly; the
        fee rate used in sizing (IV-11) is never below the conservative
        configured constant."""
        self._last_limits = self.clock.monotonic()
        try:
            limits = await self.adapter.get_limits()
        except ExchangeError as exc:
            logger.warning("limits unavailable: %s", exc)
            if self.engine.fee_rate is None:
                self.engine.fee_rate = self.s.fee_rate_fallback
            return
        self.engine.fee_rate = max(limits.taker_fee or 0.0, self.s.fee_rate_fallback)
        headroom = limits.headroom
        if headroom is not None:
            self.metrics.gauge("rate_limit_headroom", headroom)
            if headroom <= 0.0:
                self.health.set_condition("rate_limit", "address-based rate limit exhausted", level=BLOCK, auto_resume=True, alert=True)
            else:
                if not self.queue.throttled:
                    self.queue.clear_throttle()
                if headroom < RATE_HEADROOM_ALERT:
                    self.notifier.alert("Rate-limit headroom low", f"{headroom:.0%} of the address-based budget left", critical=False)

    def _clock_drift(self, view: ExchangeView) -> None:
        """NFR-015. Each sample is local receive time minus the exchange
        timestamp: clock offset plus latency. The smallest sample in the
        window is the closest estimate of the offset alone."""
        if view.account.exchange_ms is None:
            return
        self._drift.append(view.account.received_ms - view.account.exchange_ms)
        if len(self._drift) < 3:
            return
        estimate = min(self._drift)  # latency only ever adds, so the minimum is nearest the true offset
        self.metrics.gauge("clock_drift_ms", estimate)
        if abs(estimate) > CLOCK_DRIFT_PAUSE_MS:
            self.health.set_condition("clock_drift", f"local clock differs from exchange time by about {estimate} ms", level=PAUSE, auto_resume=True)
        else:
            self.health.clear_condition("clock_drift")

    def _snapshots(self, view: ExchangeView) -> None:
        """FR-073: position and account snapshots, written when a
        position changes and at most once a minute otherwise."""
        now = self.clock.monotonic()
        sizes = {c: view.account.szi(c) for c in self.s.coins}
        if sizes == self._last_positions and now - self._last_snapshot < SNAPSHOT_EVERY_S:
            return
        self._last_positions = sizes
        self._last_snapshot = now
        stamp = self.clock.iso()
        with self.db.tx():
            for dex, acct in view.account.accounts_once().items():  # a unified balance is stored once, not once per dex
                self.db.insert(
                    "account_snapshots", env=self.s.env, dex=dex, account_value=acct.account_value,
                    margin_used=acct.margin_used, total_ntl=acct.total_notional, withdrawable=acct.withdrawable, snapshot_at=stamp,
                )
            for coin in self.s.coins:
                position = view.account.position(coin)
                self.db.insert(
                    "position_snapshots", coin=coin, szi=position.szi if position else 0.0,
                    entry_px=position.entry_px if position else None, unrealized_pnl=position.unrealized_pnl if position else 0.0,
                    leverage=position.leverage if position else None, liquidation_px=position.liquidation_px if position else None,
                    margin_used=position.margin_used if position else 0.0, cum_funding=position.cum_funding if position else 0.0,
                    at=stamp, source="rest",
                )

    async def _foreign_state(self, view: ExchangeView) -> None:
        """FR-060: anything on a managed asset the bot did not create
        blocks entries on that asset, raises an alert and is never
        modified. A bot order left open after its trade closed is ours
        and is cancelled."""
        known = {r["cloid"]: r for r in self.db.all("SELECT o.cloid, o.state, o.trade_id, t.state AS trade_state FROM orders o JOIN trades t ON t.id = o.trade_id")}
        orphans: dict[str, list[str]] = {}
        for coin in self.s.coins:
            tol = self.engine._tol(coin)
            holding = self.engine.holding_trades(coin)
            entering = [t for t in self.engine.open_trades() if t["coin"] == coin and t["state"] == "ENTERING"]
            reasons: list[str] = []
            if abs(view.account.szi(coin)) > tol and not holding and not entering:
                reasons.append(f"position {view.account.szi(coin):g}")
            for order in view.open_orders:
                if order.coin != coin:
                    continue
                mine = known.get(order.cloid) if order.cloid else None
                if mine is None:
                    reasons.append(f"order {order.oid}")
                elif mine["trade_state"] in TRADE_TERMINAL and order.cloid:
                    orphans.setdefault(coin, []).append(order.cloid)
            key = f"foreign_state:{coin}"
            if reasons:
                self.health.set_condition(
                    key, f"{coin}: not created by the bot: {', '.join(reasons[:5])}", level=BLOCK, scope=coin, auto_resume=True, alert=True
                )
                self.metrics.inc("foreign_state_detections")
            else:
                self.health.clear_condition(key)
        for coin, cloids in orphans.items():
            self.db.audit("order.orphan_cancel", {"coin": coin, "cloids": cloids})
            try:
                await self.queue.submit(Priority.CANCEL, "cancelByCloid", self._cancel_call(coin, cloids), audit={"cloids": cloids, "reason": "orphan"})
            except ExchangeError as exc:
                logger.warning("orphan cancel failed on %s: %s", coin, exc)

    def _cancel_call(self, coin: str, cloids: list[str]) -> Any:
        async def call(nonce: int) -> Any:
            return await self.adapter.cancel(coin, cloids, nonce)

        return call

    def _account_guards(self, view: ExchangeView) -> None:
        """§10.7: daily loss limit, margin usage above the cap."""
        dexes = {self.engine.instruments[c].dex for c in self.s.coins if c in self.engine.instruments}
        over: list[str] = []
        for dex in dexes:
            acct = view.account.dex_account(dex)
            if acct is None:
                continue
            label = dex or "default"
            for coin in self.s.coins:
                inst = self.engine.instruments.get(coin)
                if inst is None or inst.dex != dex:
                    continue
                if acct.account_value <= 0:
                    # Say what is actually wrong, rather than letting every signal fail a risk check.
                    # Scoped to the coin: an unfunded dex says nothing about markets on other dexes.
                    self.health.set_condition(
                        f"no_collateral:{coin}",
                        f"no collateral on the {label} perp dex (account value 0), so {coin} cannot be traded: "
                        "fund that dex with its collateral token",
                        level=BLOCK, scope=coin, auto_resume=True, alert=True,
                    )
                else:
                    self.health.clear_condition(f"no_collateral:{coin}")
            row = self.engine.day.ensure(acct.account_value)
            if row is not None and row["start_equity"] <= 0:
                self.db.execute(
                    "UPDATE day_state SET loss_limit_hit_at = NULL, target_hit_at = NULL WHERE utc_date = ?", (row["utc_date"],)
                )
            if acct.account_value > 0 and acct.margin_used / acct.account_value > self.s.risk.margin_usage_cap + 1e-9:
                over.append(f"{dex or 'default'} {acct.margin_used / acct.account_value:.0%}")
            pnl = self.engine.day.pnl(view.account, self.s.coins)
            if pnl is not None:
                self.metrics.gauge("day_pnl", pnl)
            if row and pnl is not None and daily_loss_reached(pnl, row["start_equity"], self.s.risk.max_daily_loss_pct):
                self.engine.day.mark("loss_limit_hit_at")
                self.health.set_condition(
                    "daily_loss", f"day P/L {pnl:.2f} is at or below -{self.s.risk.max_daily_loss_pct:g}% of {row['start_equity']:.2f}; "
                    "entries blocked until the next UTC day", level=BLOCK, auto_resume=True, alert=True,
                )
        if over:
            self.health.set_condition("margin_usage", f"margin usage above the cap: {', '.join(over)}", level=BLOCK, auto_resume=True, alert=True)
        else:
            self.health.clear_condition("margin_usage")
        today = self.clock.utc_date()
        day_row = self.db.one("SELECT loss_limit_hit_at FROM day_state WHERE utc_date = ?", (today,))
        if self.health.has_condition("daily_loss") and (day_row is None or not day_row["loss_limit_hit_at"]):
            self.health.clear_condition("daily_loss")  # 00:00 UTC rollover

    def _unknown_orders(self) -> None:
        """§10.7: any order unresolved beyond its deadline + 10 s pauses entries."""
        marks = ", ".join("?" for _ in ORDER_UNRESOLVED)
        rows = self.db.all(f"SELECT * FROM orders WHERE state IN ({marks})", tuple(ORDER_UNRESOLVED))
        self.metrics.gauge("open_unknown_orders", len([r for r in rows if r["state"] == "UNKNOWN"]))
        now_ms = self.clock.now_ms()
        late = []
        for row in rows:
            created_ms = int(parse_iso(row["created_at"]).timestamp() * 1000)
            deadline = int(row["expires_after"] or (created_ms + self.s.entry_ttl_ms))
            if now_ms > deadline + 15000:
                late.append(row["cloid"])
        if late:
            self.health.set_condition("order_unknown", f"{len(late)} order(s) unresolved past their deadline", level=PAUSE, auto_resume=True)
        else:
            self.health.clear_condition("order_unknown")

    # ================================================================== one pass
    async def run_once(self, reason: str = "timer") -> dict[str, Any]:
        async with self._lock:
            started = self.clock.monotonic()
            try:
                view = await self.engine.view()
                new_fills = await self.engine.sync_fills()
            except ExchangeError as exc:
                self.metrics.inc("reconcile_failures")
                if self._unreachable_since is None:
                    self._unreachable_since = started
                elif started - self._unreachable_since >= UNREACHABLE_PAUSE_S:
                    self.health.set_condition(
                        "exchange_unreachable", f"account state unreadable for {started - self._unreachable_since:.0f} s: {exc.kind.value}",
                        level=PAUSE, auto_resume=True,
                    )
                return {"ok": False, "error": exc.kind.value}
            self._unreachable_since = None
            self.health.clear_condition("exchange_unreachable")
            self.last_view = view
            self._clock_drift(view)
            self._snapshots(view)
            await self._foreign_state(view)
            await self.engine.drive_all(view)
            self._account_guards(view)
            self._unknown_orders()
            if self.health.has_condition("recovering") and reason != "startup" and self.recovery_invariant_ok():
                self.health.clear_condition("recovering")
            now = self.clock.monotonic()
            if now - self._last_instruments >= INSTRUMENT_REFRESH_S:
                await self.refresh_instruments()
            if now - self._last_limits >= LIMITS_REFRESH_S:
                await self.check_limits()
                await self.check_signer()
            self.last_ok_at = self.clock.iso()
            self.metrics.inc("reconcile_passes")
            self.metrics.observe("reconcile_ms", (self.clock.monotonic() - started) * 1000.0)
            self.metrics.gauge("rest_weight_last_minute", self.adapter.rest_weight_last_minute())
            self.health.refresh()
            return {"ok": True, "reason": reason, "new_fills": new_fills, "open_trades": len(self.engine.open_trades())}

    async def loop(self, stop: asyncio.Event) -> None:
        """Timer (default 15 s), plus on demand after a reconnect or a command."""
        while not stop.is_set():
            self._kick.clear()
            try:
                await self.run_once("timer")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconciliation must keep running
                logger.exception("reconciliation pass failed")
                self.metrics.inc("reconcile_failures")
            await wait_event(self._kick, self.s.reconcile_interval_s)

    def status(self) -> dict[str, Any]:
        live = [r for r in self.db.all("SELECT state, COUNT(*) AS n FROM orders GROUP BY state") if r["state"] not in ORDER_TERMINAL]
        return {"last_ok_at": self.last_ok_at, "live_orders": {r["state"]: r["n"] for r in live}}
