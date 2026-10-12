"""Execution engine: entry, protection, monitoring and closure of trades
(PRD §10.1, §10.4, §10.5, §12.3 - §12.5; FR-047 .. FR-064).

Design rules, each traceable to the PRD:

* **Intent before send.** Every order row is committed (state
  ``INTENT``, deterministic cloid) before the request leaves the
  process.
* **Responses are provisional.** A fill is confirmed against order
  status and fills; a stop-loss counts only once the exchange's own
  open-order list shows it.
* **Unknown is a state, not a retry.** A timed-out request is resolved
  by client order id, fills and position before anything related is
  sent again.
* **Position open and position protected are different states** with a
  soft and a hard deadline; past the hard deadline the position is
  flattened.
* **Protective actions never ask permission.** Nothing in the protect,
  close or cancel paths consults the pause state.

Each open trade is driven by one small loop that calls ``drive()``, an
idempotent step function keyed on the persisted trade state. Recovery
after a crash is therefore just "start the loop again".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

from hlbot.config import Settings
from hlbot.core.action_queue import ActionQueue, Priority
from hlbot.core.daystate import DayState
from hlbot.core.health import BLOCK, HALT, PAUSE, Health
from hlbot.core.metrics import Metrics
from hlbot.core.notifier import Notifier
from hlbot.db.database import Database, Row, dumps, loads
from hlbot.domain import outcome as outcomes
from hlbot.domain.cloid import make_cloid, signal_key
from hlbot.domain.planning import entry_limit_price, is_expired, is_price_in_range, parse_iso
from hlbot.domain.risk import (
    AccountView,
    Candidate,
    InstrumentSpec,
    MarketView,
    StateView,
    assess,
    build_ladder,
)
from hlbot.domain.rounding import floor_size, round_price, size_step, tick_size
from hlbot.domain.states import (
    CANCEL_REASON_ALERT,
    ORDER_TERMINAL,
    TRADE_HOLDING,
    TRADE_TERMINAL,
    can_transition,
    map_exchange_status,
)
from hlbot.exchange.base import (
    AccountState,
    ErrorClass,
    ExchangeAdapter,
    ExchangeError,
    Fill,
    Instrument,
    MarketContext,
    OpenOrder,
    OrderRequest,
    OrderUpdate,
    OutcomeUnknown,
    PlaceResult,
    Quote,
)
from hlbot.exchange.errors import ENTRY_BLOCK_ASSET, ENTRY_REJECT_RISK
from hlbot.logging_setup import new_correlation_id
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.engine")

FILL_OVERLAP_MS = 10 * 60 * 1000
RESOLVE_GRACE_MS = 5000
UNKNOWN_PAUSE_MS = 10000
BOT_CLOSE_REASONS = frozenset({"manual", "kill"})
FAST_STATES = frozenset({"ENTERING", "OPEN_UNPROTECTED", "FLATTENING", "CLOSING"})
MAX_TP_TRIES = 3
REST_FAILURES_TO_BLOCK = 3


@dataclass
class ExchangeView:
    """One consistent look at the exchange, shared by a reconciliation pass."""

    account: AccountState
    open_orders: list[OpenOrder]

    def open_by_cloid(self) -> dict[str, OpenOrder]:
        return {o.cloid: o for o in self.open_orders if o.cloid}


class Engine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        adapter: ExchangeAdapter,
        queue: ActionQueue,
        health: Health,
        notifier: Notifier,
        clock: Optional[Clock] = None,
        metrics: Optional[Metrics] = None,
        *,
        fast_poll_s: float = 0.4,
        halted_poll_s: float = 15.0,
    ):
        self.s = settings
        self.db = db
        self.adapter = adapter
        self.queue = queue
        self.health = health
        self.notifier = notifier
        self.clock = clock or Clock()
        self.metrics = metrics or Metrics()
        self.day = DayState(db, self.clock)
        self.fast_poll_s = fast_poll_s
        self.halted_poll_s = halted_poll_s
        self.instruments: dict[str, Instrument] = {}
        self.quotes: dict[str, Quote] = {}
        self.contexts: dict[str, MarketContext] = {}
        self.fee_rate: Optional[float] = None
        self.last_account: Optional[AccountState] = None
        self.last_view: Optional[ExchangeView] = None  # most recent look at the exchange, for display
        self.entries_enabled = False  # set by the app once startup recovery completes
        self._loops: dict[int, asyncio.Task] = {}
        self._wake: dict[int, asyncio.Event] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._since: dict[tuple[int, str], float] = {}
        self._sl_tries: dict[int, int] = {}
        self._alerted: set = set()
        self._wait_notes: dict[int, str] = {}
        self._leverage_ok: set = set()
        self._entry_lock = asyncio.Lock()
        self._rest_failures = 0
        self._kill = False

    # ================================================================== small helpers
    def _tol(self, coin: str) -> float:
        inst = self.instruments.get(coin)
        return size_step(inst.sz_decimals) / 2.0 if inst else 1e-9

    def _szd(self, coin: str) -> int:
        return self.instruments[coin].sz_decimals

    def _trade(self, trade_id: int) -> Row:
        row = self.db.get("trades", trade_id)
        assert row is not None, f"trade {trade_id} not found"
        return row

    def _order(self, order_id: int) -> Row:
        row = self.db.get("orders", order_id)
        assert row is not None, f"order {order_id} not found"
        return row

    def _orders(self, trade_id: int, role: Optional[str] = None, *, live_only: bool = False) -> list[Row]:
        rows = self.db.all("SELECT * FROM orders WHERE trade_id = ? ORDER BY id", (trade_id,))
        if role:
            rows = [r for r in rows if r["role"] == role]
        if live_only:
            rows = [r for r in rows if r["state"] not in ORDER_TERMINAL]
        return rows

    def _live_order(self, trade_id: int, role: str) -> Optional[Row]:
        rows = self._orders(trade_id, role, live_only=True)
        return rows[-1] if rows else None

    def holding_trades(self, coin: Optional[str] = None) -> list[Row]:
        marks = ", ".join("?" for _ in TRADE_HOLDING)
        rows = self.db.all(f"SELECT * FROM trades WHERE state IN ({marks}) ORDER BY id", tuple(TRADE_HOLDING))
        return [r for r in rows if coin is None or r["coin"] == coin]

    def open_trades(self) -> list[Row]:
        marks = ", ".join("?" for _ in TRADE_TERMINAL)
        return self.db.all(f"SELECT * FROM trades WHERE state NOT IN ({marks}) ORDER BY id", tuple(TRADE_TERMINAL))

    def _move_order(self, order_id: int, new: str, **fields: Any) -> bool:
        """Transition an order when legal; otherwise only update fields."""
        row = self.db.get("orders", order_id)
        assert row is not None
        if row["state"] == new or row["state"] in ORDER_TERMINAL or can_transition("order", row["state"], new):
            return self.db.set_order_state(order_id, new, **fields)
        if fields:
            self.db.update("orders", order_id, updated_at=self.clock.iso(), **fields)
        return False

    def _rest_failed(self, where: str, exc: Exception) -> None:
        self._rest_failures += 1
        self.metrics.inc("rest_errors")
        logger.warning("REST read failed in %s: %s", where, exc)
        if self._rest_failures >= REST_FAILURES_TO_BLOCK:
            self.health.set_condition(
                "rest_errors", f"{self._rest_failures} consecutive REST failures", level=BLOCK, auto_resume=True, alert=True
            )

    def _rest_ok(self) -> None:
        if self._rest_failures:
            self._rest_failures = 0
            self.health.clear_condition("rest_errors")

    async def view(self) -> ExchangeView:
        """Fresh account and open orders. Raises ExchangeError on failure."""
        try:
            account = await self.adapter.get_account()
            open_orders = await self.adapter.get_open_orders()
        except ExchangeError as exc:
            self._rest_failed("view", exc)
            raise
        self._rest_ok()
        self.last_account = account
        self.last_view = ExchangeView(account, open_orders)
        return self.last_view

    def _alert_once(self, key: str, title: str, detail: str = "", critical: bool = True) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            self.notifier.alert(title, detail, critical=critical)

    # ================================================================== market data in
    async def on_quote(self, quote: Quote) -> None:
        self.quotes[quote.coin] = quote

    async def on_context(self, context: MarketContext) -> None:
        self.contexts[context.coin] = context

    def quote_age_ms(self, coin: str) -> Optional[float]:
        quote = self.quotes.get(coin)
        return None if quote is None else float(self.clock.now_ms() - quote.received_ms)

    async def _mark(self, coin: str) -> Optional[float]:
        ctx = self.contexts.get(coin)
        if ctx and ctx.mark and self.clock.now_ms() - ctx.received_ms <= self.s.ws_stale_ms:
            return ctx.mark
        try:
            ctx = await self.adapter.get_market(coin)
            self.contexts[coin] = ctx
            if ctx.mark:
                return ctx.mark
        except ExchangeError as exc:
            self._rest_failed("mark", exc)
        quote = self.quotes.get(coin)
        if quote and quote.bid and quote.ask:
            return (quote.bid + quote.ask) / 2.0
        return ctx.mark if ctx else None

    # ================================================================== order plumbing
    def _order_intent(
        self,
        trade: Row,
        role: str,
        leg: int,
        *,
        is_buy: bool,
        size: float,
        price: float,
        kind: str = "limit",
        tif: Optional[str] = "Gtc",
        trigger_px: Optional[float] = None,
        reduce_only: bool = True,
        expires_after: Optional[int] = None,
    ) -> Row:
        """Persist an order intent (FR-047). The cloid attempt number is
        derived from what is already stored, so a restarted process
        derives the same id for the same intent."""
        signal = self.db.get("signals", trade["signal_id"])
        assert signal is not None
        attempt = 1 + int(
            self.db.scalar(
                "SELECT COUNT(*) FROM orders o JOIN trades t ON t.id = o.trade_id "
                "WHERE t.signal_id = ? AND o.role = ? AND o.leg = ?",
                (trade["signal_id"], role, leg),
                default=0,
            )
        )
        cloid = make_cloid(
            self.s.env, self.s.account_key, signal_key(signal["channel_id"], signal["message_id"]), role, leg, attempt
        )
        now = self.clock.iso()
        with self.db.tx():
            order_id = self.db.insert(
                "orders",
                trade_id=trade["id"],
                role=role,
                leg=leg,
                attempt=attempt,
                cloid=cloid,
                coin=trade["coin"],
                side="BUY" if is_buy else "SELL",
                type=kind,
                tif=tif if kind == "limit" else None,
                reduce_only=1 if reduce_only else 0,
                price=price,
                trigger_price=trigger_px,
                size=size,
                expires_after=expires_after,
                state="INTENT",
                created_at=now,
                updated_at=now,
            )
            self.db.audit(
                "order.intent",
                {"role": role, "leg": leg, "attempt": attempt, "cloid": cloid, "side": "BUY" if is_buy else "SELL",
                 "size": size, "price": price, "trigger_price": trigger_px, "type": kind, "tif": tif,
                 "reduce_only": reduce_only, "expires_after": expires_after},
                signal_id=trade["signal_id"],
                trade_id=trade["id"],
                order_id=order_id,
            )
        row = self.db.get("orders", order_id)
        assert row is not None
        return row

    def _request(self, order: Row) -> OrderRequest:
        return OrderRequest(
            coin=order["coin"],
            is_buy=order["side"] == "BUY",
            size=order["size"],
            price=order["price"],
            reduce_only=bool(order["reduce_only"]),
            cloid=order["cloid"],
            kind=order["type"],
            tif=order["tif"] or "Gtc",
            trigger_px=order["trigger_price"],
            tpsl=("sl" if order["role"] == "SL" else "tp") if order["type"] == "trigger" else None,
        )

    async def _send(
        self, trade_id: int, orders: Sequence[Row], priority: Priority, expires_after: Optional[int] = None
    ) -> list[PlaceResult]:
        """Send one batch through the action queue and record the
        provisional result of every order. Never raises for an exchange
        problem: an unknown outcome comes back as status ``unknown``."""
        ids = [o["id"] for o in orders]

        async def call(nonce: int) -> list[PlaceResult]:
            stamp = self.clock.iso()
            for order_id in ids:
                self.db.set_order_state(order_id, "SUBMITTING", nonce=nonce, submitted_at=stamp)
            return await self.adapter.place([self._request(o) for o in orders], nonce, expires_after)

        audit = {"cloids": [o["cloid"] for o in orders], "roles": [o["role"] for o in orders]}
        try:
            results = await self.queue.submit(priority, "order", call, audit=audit, trade_id=trade_id, order_id=ids[0])
        except OutcomeUnknown as exc:
            for order_id in ids:
                self._move_order(order_id, "UNKNOWN", error_text=exc.text)
            self.metrics.inc("unknown_outcomes")
            return [PlaceResult("unknown") for _ in ids]
        except ExchangeError as exc:
            for order_id in ids:
                row = self.db.get("orders", order_id)
                assert row is not None
                target = "NOT_ACCEPTED" if row["state"] == "INTENT" else "REJECTED"
                self._move_order(order_id, target, exchange_status=exc.kind.value, error_text=exc.text)
            return [PlaceResult("error", error=exc) for _ in ids]
        except Exception as exc:  # noqa: BLE001 - anything else: outcome is not known
            logger.exception("unexpected failure sending orders")
            for order_id in ids:
                row = self.db.get("orders", order_id)
                if row and row["state"] == "INTENT":
                    self.db.set_order_state(order_id, "SUBMITTING")
                self._move_order(order_id, "UNKNOWN", error_text=type(exc).__name__)
            return [PlaceResult("unknown") for _ in ids]
        for order_id, result in zip(ids, results, strict=True):
            self._apply_place_result(order_id, result)
        return list(results)

    def _apply_place_result(self, order_id: int, result: PlaceResult) -> None:
        row = self.db.get("orders", order_id)
        assert row is not None
        if result.status == "resting":
            if row["state"] in ("SUBMITTING", "UNKNOWN"):
                self._move_order(order_id, "RESTING", oid=result.oid, exchange_status="open")
            elif result.oid and not row["oid"]:
                self.db.update("orders", order_id, oid=result.oid)
        elif result.status == "filled":
            filled = max(float(row["filled_size"] or 0.0), float(result.total_size or 0.0))
            fields: dict[str, Any] = {"oid": result.oid, "filled_size": filled, "exchange_status": "filled"}
            if row["avg_fill_price"] is None and result.avg_price:
                fields["avg_fill_price"] = result.avg_price
            target = "FILLED" if filled >= row["size"] - self._tol(row["coin"]) else "PARTIALLY_FILLED"
            self._move_order(order_id, target, **fields)
        elif result.status == "error" and result.error is not None:
            self._move_order(order_id, "REJECTED", exchange_status=result.error.kind.value, error_text=result.error.text)

    # ================================================================== fills
    def apply_fill(self, fill: Fill, source: str = "ws") -> bool:
        """Record one fill idempotently (keyed by ``tid``) and update the
        order and trade it belongs to. Synchronous: atomic with respect
        to every coroutine."""
        if fill.coin not in self.s.coins:
            return False
        if self.db.one("SELECT 1 FROM fills WHERE tid = ?", (fill.tid,)):
            return False
        order = None
        if fill.cloid:
            order = self.db.one("SELECT * FROM orders WHERE cloid = ?", (fill.cloid,))
        if order is None:
            order = self.db.one("SELECT * FROM orders WHERE oid = ? AND coin = ?", (fill.oid, fill.coin))
        trade_id: Optional[int] = order["trade_id"] if order else None
        if trade_id is None:
            holding = self.holding_trades(fill.coin)
            trade_id = holding[0]["id"] if holding else None
        with self.db.tx():
            self.db.insert(
                "fills",
                tid=fill.tid,
                order_id=order["id"] if order else None,
                trade_id=trade_id,
                role=order["role"] if order else None,
                oid=fill.oid,
                cloid=fill.cloid,
                coin=fill.coin,
                side="BUY" if fill.is_buy else "SELL",
                price=fill.price,
                size=fill.size,
                fee=fill.fee,
                fee_token=fill.fee_token,
                closed_pnl=fill.closed_pnl,
                crossed=None if fill.crossed is None else int(fill.crossed),
                dir=fill.dir,
                start_position=fill.start_position,
                time=fill.time_ms,
                hash=fill.hash,
                liquidation=1 if fill.liquidation else 0,
                recorded_at=self.clock.iso(),
            )
            self.db.audit(
                "fill",
                {"tid": fill.tid, "oid": fill.oid, "cloid": fill.cloid, "coin": fill.coin,
                 "side": "BUY" if fill.is_buy else "SELL", "price": fill.price, "size": fill.size, "fee": fill.fee,
                 "closed_pnl": fill.closed_pnl, "role": order["role"] if order else None,
                 "liquidation": fill.liquidation, "source": source},
                trade_id=trade_id,
                order_id=order["id"] if order else None,
            )
            if order:
                self._refresh_order_from_fills(order["id"], oid=fill.oid)
            if trade_id is not None:
                self._recompute_trade(trade_id)
            last = int(self.db.kv_get("last_fill_ms", 0) or 0)
            if fill.time_ms > last:
                self.db.kv_set("last_fill_ms", fill.time_ms)
        self.metrics.inc("fills")
        if fill.liquidation:
            self.health.set_condition("liquidation", f"liquidation fill on {fill.coin}", level=HALT, auto_resume=False)
        if trade_id is not None:
            if order is None:
                logger.warning("fill %s on %s was not produced by a bot order", fill.tid, fill.coin)
            self.wake(trade_id)
        return True

    def _refresh_order_from_fills(self, order_id: int, oid: Optional[int] = None) -> None:
        order = self.db.get("orders", order_id)
        assert order is not None
        agg = self.db.one(
            "SELECT COALESCE(SUM(size), 0) AS size, COALESCE(SUM(size * price), 0) AS notional FROM fills WHERE order_id = ?",
            (order_id,),
        )
        assert agg is not None
        filled = max(float(agg["size"]), 0.0)
        fields: dict[str, Any] = {"filled_size": max(filled, float(order["filled_size"] or 0.0))}
        if filled > 0:
            fields["avg_fill_price"] = agg["notional"] / filled
        if oid and not order["oid"]:
            fields["oid"] = oid
        target = "FILLED" if filled >= order["size"] - self._tol(order["coin"]) else "PARTIALLY_FILLED"
        if order["state"] in ORDER_TERMINAL or order["state"] == "INTENT":
            self.db.update("orders", order_id, updated_at=self.clock.iso(), **fields)
        else:
            self._move_order(order_id, target, **fields)

    def _recompute_trade(self, trade_id: int) -> None:
        """Trade size figures from its fills (PRD §12.5 rule 6). Until the
        entry's fills have arrived, the entry order's provisional filled
        size (from the exchange response) stands in."""
        trade = self._trade(trade_id)
        fills = self.db.all("SELECT role, side, size, price FROM fills WHERE trade_id = ?", (trade_id,))
        entry_side = trade["side"]
        entry_size = sum(f["size"] for f in fills if f["role"] == "ENTRY")
        entry_notional = sum(f["size"] * f["price"] for f in fills if f["role"] == "ENTRY")
        provisional = self.db.one(
            "SELECT COALESCE(SUM(filled_size), 0) AS size, MAX(avg_fill_price) AS px FROM orders WHERE trade_id = ? AND role = 'ENTRY'",
            (trade_id,),
        ) or {"size": 0.0, "px": None}
        filled = max(entry_size, float(provisional["size"] or 0.0))
        avg_entry = entry_notional / entry_size if entry_size > 0 else provisional["px"]
        closed = sum(f["size"] for f in fills if f["role"] != "ENTRY" and f["side"] != entry_side)
        added = sum(f["size"] for f in fills if f["role"] != "ENTRY" and f["side"] == entry_side)
        adjustment = float(self.db.kv_get(f"trade_adjust:{trade_id}", 0.0) or 0.0)
        remaining = max(0.0, round(filled - closed + added + adjustment, 10))
        self.db.update(
            "trades", trade_id, filled_size=round(filled, 10), avg_entry=avg_entry, remaining_size=remaining, updated_at=self.clock.iso()
        )

    async def on_fill(self, fill: Fill, is_snapshot: bool = False) -> None:
        self.apply_fill(fill, "ws_snapshot" if is_snapshot else "ws")

    async def on_order_update(self, update: OrderUpdate) -> None:
        """Order updates only wake the owning trade; the state itself is
        confirmed against REST by the drive step."""
        if not update.cloid:
            return
        row = self.db.one("SELECT id, trade_id, oid FROM orders WHERE cloid = ?", (update.cloid,))
        if row:
            if not row["oid"]:
                self.db.update("orders", row["id"], oid=update.oid)
            self.wake(row["trade_id"])

    async def on_liquidation(self, event: dict[str, Any]) -> None:
        self.db.audit("exchange.liquidation", dict(event))
        self.health.set_condition("liquidation", "liquidation event on the account", level=HALT, auto_resume=False)
        for trade in self.holding_trades():
            self.wake(trade["id"])

    async def sync_fills(self) -> int:
        """Fetch fills since the last stored fill, minus an overlap, and
        apply the new ones. Replays are harmless."""
        last = int(self.db.kv_get("last_fill_ms", 0) or 0)
        if last:
            since = last - FILL_OVERLAP_MS
        else:
            # Nothing stored yet: start just before the first trade this
            # database knows of (or a day back), never from the beginning
            # of the account's history.
            first = self.db.scalar("SELECT MIN(created_at) FROM trades")
            if first is None:
                since = self.clock.now_ms() - 24 * 3600 * 1000
            else:
                since = int(parse_iso(first).timestamp() * 1000) - FILL_OVERLAP_MS
        try:
            fills = await self.adapter.get_fills(max(0, since))
        except ExchangeError as exc:
            self._rest_failed("fills", exc)
            raise
        self._rest_ok()
        return sum(1 for fill in fills if self.apply_fill(fill, "rest"))

    # ================================================================== entry (PRD §10.1 stages 3-7)
    def _note_wait(self, signal_id: int, reason: str) -> None:
        """Record why a waiting signal was not entered, once per reason."""
        if self._wait_notes.get(signal_id) != reason:
            self._wait_notes[signal_id] = reason
            self.db.audit("signal.waiting", {"reason": reason}, signal_id=signal_id)
            logger.info("signal %s waiting: %s", signal_id, reason)

    def _duplicate_reason(self, signal: Row) -> Optional[str]:
        """FR-011 / FR-039: the same content already acted on in this
        channel, or in another channel inside the expiry window."""
        if not self.s.duplicate_protection or not signal["content_id"]:
            return None
        others = self.db.all(
            "SELECT s.id, s.channel_id, s.created_at FROM signals s WHERE s.content_id = ? AND s.id != ? "
            "AND (s.state IN ('ENTERING', 'TRADED') OR EXISTS (SELECT 1 FROM trades t WHERE t.signal_id = s.id AND t.filled_size > 0))",
            (signal["content_id"], signal["id"]),
        )
        for other in others:
            if other["channel_id"] == signal["channel_id"]:
                return f"DUPLICATE: content {signal['content_id']} already acted on (signal {other['id']})"
        for other in others:
            return f"DUPLICATE_CROSS_CHANNEL: content {signal['content_id']} already acted on from another channel (signal {other['id']})"
        return None

    def _skip_signal(self, signal: Row, state: str, reason: str) -> None:
        if self.db.set_signal_state(signal["id"], state, reason):
            self.notifier.notify(
                "signal_skipped", state=state, reason=reason, message_id=signal["message_id"], content_id=signal["content_id"]
            )
        self._wait_notes.pop(signal["id"], None)

    async def _ensure_leverage(self, coin: str) -> bool:
        """FR-037: the exchange-reported leverage equals the configured
        value before the first entry on an asset."""
        if coin in self._leverage_ok:
            return True
        want = self.s.risk.leverage
        try:
            current = await self.adapter.get_leverage(coin)
            if current != want:
                cross = self.s.margin_mode == "cross"
                await self.queue.submit(
                    Priority.ENTRY,
                    "updateLeverage",
                    lambda nonce: self.adapter.set_leverage(coin, want, cross, nonce),
                    audit={"coin": coin, "leverage": want, "cross": cross},
                )
                current = await self.adapter.get_leverage(coin)
        except ExchangeError as exc:
            logger.warning("could not set/verify leverage on %s: %s", coin, exc)
            return False
        if current != want:
            return False
        self._leverage_ok.add(coin)
        self.db.audit("leverage.verified", {"coin": coin, "leverage": want})
        return True

    async def try_enter(self, signal_id: int) -> None:
        """Evaluate one waiting signal against the current quote and, if
        everything passes, enter. Serialised: one entry decision at a
        time, so a race can never produce two intents (TC-063)."""
        if not self.entries_enabled:
            return
        async with self._entry_lock:
            decision_started = self.clock.monotonic()
            signal = self.db.get("signals", signal_id)
            if signal is None or signal["state"] != "WAITING_PRICE":
                return
            plan = self.db.one("SELECT * FROM plans WHERE signal_id = ?", (signal_id,))
            if plan is None:
                return
            coin = plan["coin"]
            now = self.clock.now()
            if is_expired(signal["expires_at"], now):
                self._skip_signal(signal, "EXPIRED", f"not entered before {signal['expires_at']}")
                return
            if signal["last_attempt_at"]:
                if (now - parse_iso(signal["last_attempt_at"])).total_seconds() < self.s.entry_retry_min_s:
                    return
            quote = self.quotes.get(coin)
            if quote is None or quote.bid is None or quote.ask is None:
                self._note_wait(signal_id, "DATA_STALE: no quote")
                return
            direction = signal["side"]
            ref = quote.ask if direction == "BUY" else quote.bid
            if not is_price_in_range(ref, plan["effective_low"], plan["effective_high"]):
                self._wait_notes.pop(signal_id, None)
                return
            allowed, why = self.health.can_enter(coin)
            if not allowed:
                self._note_wait(signal_id, str(why))
                return
            quote_age = float(self.clock.now_ms() - quote.received_ms)
            if quote_age > self.s.risk.max_quote_age_ms:
                self._note_wait(signal_id, f"DATA_STALE: quote age {quote_age:.0f} ms")
                return

            new_correlation_id("entry-")
            duplicate = self._duplicate_reason(signal)
            if duplicate:
                self._skip_signal(signal, "DUPLICATE", duplicate)
                return
            inst = self.instruments.get(coin)
            if inst is None or inst.is_delisted:
                self._skip_signal(signal, "REJECTED_RISK", f"ASSET_UNAVAILABLE: {coin} is unknown or delisted")
                return
            tp_levels = loads(plan["tp_levels"], [])
            limit = entry_limit_price(
                direction, ref, self.s.max_slippage_points, float(tp_levels[0]["price"]), self.s.min_tp_gap_points, inst.sz_decimals
            )
            if limit is None:
                self._note_wait(signal_id, "LIMIT_CLAMP: the limit clamped against TP1 cannot cross the spread")
                return

            # ---- fresh account state, fail closed (FR-042)
            account: Optional[AccountState] = None
            try:
                account = await self.adapter.get_account()
                self._rest_ok()
                self.last_account = account
            except ExchangeError as exc:
                self._rest_failed("entry account", exc)
            context = self.contexts.get(coin)
            if context is None or self.clock.now_ms() - context.received_ms > self.s.ws_stale_ms:
                try:
                    context = await self.adapter.get_market(coin)
                    self.contexts[coin] = context
                except ExchangeError as exc:
                    self._rest_failed("entry market", exc)
                    context = None

            holding = self.holding_trades()
            if account is not None and abs(account.szi(coin)) > self._tol(coin) and not [t for t in holding if t["coin"] == coin]:
                # FR-060: a position the bot did not create blocks entries and is never touched.
                self.health.set_condition(
                    f"foreign_state:{coin}",
                    f"position of {account.szi(coin):g} on {coin} was not opened by the bot",
                    level=BLOCK, scope=coin, auto_resume=True, alert=True,
                )
                self._note_wait(signal_id, f"FOREIGN_POSITION on {coin}")
                return

            dex = account.dex_account(inst.dex) if account else None
            day_row = self.day.ensure(dex.account_value if dex else None)
            entering = [t for t in self.open_trades() if t["state"] == "ENTERING"]
            assessment = assess(
                Candidate(coin, direction, ref, limit, float(plan["sl"]), tp_levels),
                InstrumentSpec(inst.sz_decimals, inst.max_leverage),
                AccountView(
                    equity=dex.account_value if dex else None,
                    available_margin=dex.available_margin if dex else None,
                    total_notional=dex.total_notional if dex else None,
                    maintenance_margin_used=dex.maintenance_margin_used if dex else None,
                    age_ms=float(self.clock.now_ms() - account.received_ms) if account else None,
                ),
                MarketView(
                    mark=context.mark if context else None,
                    oracle=context.oracle if context else None,
                    quote_age_ms=float(self.clock.now_ms() - quote.received_ms),
                ),
                StateView(
                    open_trades_total=len(holding) + len(entering),
                    open_trades_on_asset=len([t for t in holding + entering if t["coin"] == coin]),
                    day_pnl=self.day.pnl(account, self.s.coins),
                    day_start_equity=day_row["start_equity"] if day_row else None,
                    fee_rate=self.fee_rate,
                ),
                self.s.risk,
            )
            attempt = int(signal["attempts"]) + 1
            if not assessment.approved and assessment.transient_only:
                self._note_wait(signal_id, "; ".join(f"{v.rule}: {v.detail}" for v in assessment.violations))
                return
            assessment_id = self.db.insert(
                "risk_assessments",
                signal_id=signal_id,
                attempt=attempt,
                at=self.clock.iso(),
                inputs=dumps(assessment.inputs),
                outputs=dumps(assessment.outputs()),
                approved=1 if assessment.approved else 0,
                violations=dumps([{"rule": v.rule, "detail": v.detail} for v in assessment.violations]),
            )
            if not assessment.approved:
                rules = assessment.rules()
                reason = "; ".join(f"{v.rule}: {v.detail}" for v in assessment.violations)
                if "DAILY_LOSS" in rules and self.day.mark("loss_limit_hit_at"):
                    self.health.set_condition(
                        "daily_loss", "daily loss limit reached; entries blocked until the next UTC day",
                        level=BLOCK, auto_resume=True, alert=True,
                    )
                if "DAILY_TARGET" in rules:
                    self.day.mark("target_hit_at")
                    if self.day.mark("target_notified"):
                        self.notifier.notify(
                            "daily_target_reached", today_pnl=self.day.pnl(account, self.s.coins),
                            daily_target_percent=self.s.risk.daily_target_pct, symbol=signal["symbol"],
                            direction=direction, content_id=signal["content_id"],
                        )
                    self.db.set_signal_state(signal_id, "REJECTED_RISK", reason)
                    return
                self._skip_signal(signal, "REJECTED_RISK", reason)
                return

            if not await self._ensure_leverage(coin):
                self._note_wait(signal_id, "LEVERAGE_UNVERIFIED: exchange leverage does not match the configured value")
                return

            # ---- the awaits above took time: decide on the latest quote only
            quote = self.quotes.get(coin)
            signal = self.db.get("signals", signal_id)
            if signal is None or signal["state"] != "WAITING_PRICE" or quote is None or quote.bid is None or quote.ask is None:
                return
            latest = quote.ask if direction == "BUY" else quote.bid
            crossable = latest <= limit if direction == "BUY" else latest >= limit
            if (
                not is_price_in_range(latest, plan["effective_low"], plan["effective_high"])
                or not crossable
                or self.clock.now_ms() - quote.received_ms > self.s.risk.max_quote_age_ms
                or is_expired(signal["expires_at"], self.clock.now())
                or not self.health.can_enter(coin)[0]
            ):
                self._note_wait(signal_id, "MOVED: market or state changed during pre-trade checks")
                return

            # ---- intent before send (FR-047)
            now_iso = self.clock.iso()
            expires_after = self.clock.now_ms() + self.s.entry_ttl_ms
            with self.db.tx():
                trade_id = self.db.insert(
                    "trades",
                    signal_id=signal_id,
                    risk_assessment_id=assessment_id,
                    attempt=attempt,
                    env=self.s.env,
                    coin=coin,
                    side=direction,
                    state="ENTERING",
                    planned_size=assessment.size,
                    limit_price=limit,
                    sl_price=assessment.sl_price,
                    tp_plan=dumps(assessment.legs),
                    config_version=plan["config_version"],
                    created_at=now_iso,
                    updated_at=now_iso,
                )
                self.db.audit("trade.state", {"from": None, "to": "ENTERING"}, signal_id=signal_id, trade_id=trade_id)
                order = self._order_intent(
                    self._trade(trade_id), "ENTRY", 0, is_buy=direction == "BUY", size=assessment.size, price=limit,
                    kind="limit", tif="Ioc", reduce_only=False, expires_after=expires_after,
                )
                self.db.set_signal_state(signal_id, "ENTERING", None, attempts=attempt, last_attempt_at=now_iso)
            self._wait_notes.pop(signal_id, None)
            self.metrics.inc("entries_submitted")
            # NFR-002: in-range quote -> order handed to the queue (includes the pre-trade reads).
            self.metrics.observe("decision_to_submit_ms", (self.clock.monotonic() - decision_started) * 1000.0)
            received = self.db.scalar(
                "SELECT received_at FROM messages WHERE channel_id = ? AND message_id = ?",
                (signal["channel_id"], signal["message_id"]),
            )
            if received:
                self.metrics.observe("signal_to_intent_ms", (self.clock.now() - parse_iso(received)).total_seconds() * 1000.0)

            result = (await self._send(trade_id, [order], Priority.ENTRY, expires_after))[0]
            if result.status == "unknown":
                self._ensure_loop(trade_id)  # resolved by cloid before anything is retried
            elif result.status == "error" and result.error is not None:
                await self._entry_failed(trade_id, result.error)
            else:
                await self._confirm_entry(trade_id, result)

    async def _entry_failed(self, trade_id: int, error: ExchangeError) -> None:
        """An entry produced no fill. Decide what the signal does next (§13.4)."""
        trade = self._trade(trade_id)
        signal = self.db.get("signals", trade["signal_id"])
        assert signal is not None
        coin = trade["coin"]
        if trade["state"] == "ENTERING":
            self.db.set_trade_state(trade_id, "ABORTED", close_reason=error.kind.value, closed_at=self.clock.iso())
        kind = error.kind
        if kind in ENTRY_BLOCK_ASSET:
            self.health.set_condition(
                f"asset_blocked:{coin}", f"{coin}: {error.text}", level=BLOCK, scope=coin, auto_resume=True, alert=True
            )
        if kind == ErrorClass.AUTH:
            self.health.set_condition(
                "agent_key", f"exchange rejected the signer: {error.text}", level=PAUSE, auto_resume=False
            )
        if kind in ENTRY_REJECT_RISK:
            self._skip_signal(signal, "FAILED", f"exchange rejected the entry: {error.text}")
            self.notifier.alert("Entry rejected by exchange (margin)", f"{coin}: {error.text}", critical=False)
            return
        if kind == ErrorClass.LOCAL_BUG:
            self._skip_signal(signal, "FAILED", f"order failed exchange validation: {error.text}")
            self.notifier.alert("DEFECT: order failed exchange validation", f"{coin}: {error.text}", critical=True)
            return
        if kind in (ErrorClass.REDUCE_ONLY, ErrorClass.INVALID_TPSL, ErrorClass.OTHER):
            self._skip_signal(signal, "FAILED", f"exchange rejected the entry: {error.text}")
            return
        # Transient: zero fill, no liquidity, far from oracle, expired, throttled, lost.
        if int(signal["attempts"]) >= self.s.entry_attempts:
            self._skip_signal(signal, "FAILED", f"entry attempts exhausted ({signal['attempts']}): {error.text or kind.value}")
        else:
            self.db.set_signal_state(signal["id"], "WAITING_PRICE", f"attempt {signal['attempts']}: {kind.value}")

    async def _confirm_entry(self, trade_id: int, provisional: Optional[PlaceResult]) -> None:
        """FR-049: confirm the fill against order status and fills, then
        hand the trade to the protection protocol."""
        trade = self._trade(trade_id)
        if trade["state"] != "ENTERING":
            return
        order = self._orders(trade_id, "ENTRY")[-1]
        coin = trade["coin"]
        tol = self._tol(coin)
        status = None
        try:
            status = await self.adapter.get_order(order["cloid"])
            since = int(parse_iso(order["created_at"]).timestamp() * 1000) - 2000
            for fill in await self.adapter.get_fills(since):
                self.apply_fill(fill, "rest")
            self._rest_ok()
        except ExchangeError as exc:
            self._rest_failed("confirm entry", exc)
        order = self._order(order["id"])
        assert order is not None
        filled = float(order["filled_size"] or 0.0)
        if status is not None and status.found and status.filled_size is not None and status.filled_size > filled:
            filled = status.filled_size
            self.db.update("orders", order["id"], filled_size=filled, oid=status.oid or order["oid"])
        self._recompute_trade(trade_id)

        if filled <= tol:
            if order["state"] not in ORDER_TERMINAL:
                self._move_order(order["id"], "REJECTED", exchange_status=(status.status if status and status.found else "iocCancelRejected"))
            await self._entry_failed(trade_id, ExchangeError(ErrorClass.NO_MATCH, "IOC entry had no fill"))
            return
        order = self._order(order["id"])
        assert order is not None
        if order["state"] == "PARTIALLY_FILLED":
            # An IOC never rests: the unfilled remainder is cancelled.
            self._move_order(order["id"], "CANCELED", exchange_status=(status.status if status and status.found else "iocCanceled"))

        trade = self._trade(trade_id)
        self._since[(trade_id, "unprotected")] = self.clock.monotonic()
        self._sl_tries[trade_id] = 0
        self.db.set_trade_state(trade_id, "OPEN_UNPROTECTED", opened_at=self.clock.iso())
        self.db.set_signal_state(trade["signal_id"], "TRADED")
        self.metrics.inc("entries_filled")
        partial = trade["filled_size"] < trade["planned_size"] - tol
        if partial:
            self.db.audit(
                "trade.partial_fill", {"planned": trade["planned_size"], "filled": trade["filled_size"], "policy": self.s.partial_fill_policy},
                trade_id=trade_id,
            )
        if self._kill:
            self._to_flatten(trade_id, "kill")
        elif partial and self.s.partial_fill_policy == "flatten":
            self._to_flatten(trade_id, "partial_fill_policy")
        else:
            plan = self.db.one("SELECT tp_levels FROM plans WHERE signal_id = ?", (trade["signal_id"],))
            legs = build_ladder(
                trade["side"], trade["filled_size"], loads(plan["tp_levels"], []) if plan else [], self._szd(coin), self.s.risk.min_notional
            )
            if legs is None:
                # FR-053: a fill too small to protect is always flattened.
                self._to_flatten(trade_id, "too_small")
            else:
                self.db.update("trades", trade_id, tp_plan=dumps(legs))
        # Hand over to the trade's own loop. Never drive from here: this
        # method also runs inside drive() (resolution after a lost
        # response or a restart), which already holds the trade lock.
        self._ensure_loop(trade_id)
        self.wake(trade_id)

    # ================================================================== unknown outcomes (PRD §9.6.4)
    async def _resolve_unknown(self, order: Row) -> str:
        """Returns ``found``, ``pending``, ``not_accepted`` or ``unreconciled``."""
        coin = order["coin"]
        try:
            status = await self.adapter.get_order(order["cloid"])
        except ExchangeError as exc:
            self._rest_failed("resolve", exc)
            return "pending"
        self._rest_ok()
        created_ms = int(parse_iso(order["created_at"]).timestamp() * 1000)
        if status.found:
            local = map_exchange_status(status.status or "")
            if status.oid and not order["oid"]:
                self.db.update("orders", order["id"], oid=status.oid)
            if local == "FILLED" or (status.filled_size or 0.0) > self._tol(coin):
                try:
                    for fill in await self.adapter.get_fills(created_ms - 2000):
                        self.apply_fill(fill, "rest")
                except ExchangeError as exc:
                    self._rest_failed("resolve fills", exc)
                current = self.db.get("orders", order["id"])
                assert current is not None
                filled = max(float(current["filled_size"] or 0.0), float(status.filled_size or 0.0))
                target = "FILLED" if local == "FILLED" or filled >= order["size"] - self._tol(coin) else "PARTIALLY_FILLED"
                self._move_order(order["id"], target, filled_size=filled, exchange_status=status.status)
                if target == "PARTIALLY_FILLED" and local == "CANCELED":
                    self._move_order(order["id"], "CANCELED", exchange_status=status.status)
            elif local == "TRIGGERED":
                self._move_order(order["id"], "RESTING", exchange_status="open")
                self._move_order(order["id"], "TRIGGERED", exchange_status=status.status)
            else:
                self._move_order(order["id"], local, exchange_status=status.status)
            self.db.audit("order.resolved", {"cloid": order["cloid"], "status": status.status}, trade_id=order["trade_id"], order_id=order["id"])
            return "found"

        # unknownOid: an entry may still land until its deadline has passed.
        now_ms = self.clock.now_ms()
        if order["role"] == "ENTRY":
            deadline = int(order["expires_after"] or (created_ms + self.s.entry_ttl_ms)) + RESOLVE_GRACE_MS
            if now_ms < deadline:
                return "pending"
        try:
            fills = await self.adapter.get_fills(created_ms - 2000)
        except ExchangeError as exc:
            self._rest_failed("resolve fills", exc)
            return "pending"
        mine = [f for f in fills if f.cloid == order["cloid"] or (order["oid"] and f.oid == order["oid"])]
        if mine:
            for fill in mine:
                self.apply_fill(fill, "rest")
            self.db.audit("order.resolved", {"cloid": order["cloid"], "status": "fills_found"}, trade_id=order["trade_id"], order_id=order["id"])
            current = self.db.get("orders", order["id"])
            if current and current["state"] == "UNKNOWN":
                self._refresh_order_from_fills(order["id"])
            return "found"
        if order["role"] == "ENTRY":
            try:
                account = await self.adapter.get_account()
            except ExchangeError as exc:
                self._rest_failed("resolve account", exc)
                return "pending"
            others = [t for t in self.holding_trades(coin) if t["id"] != order["trade_id"]]
            if abs(account.szi(coin)) > self._tol(coin) and not others:
                # Position exists but neither the order nor a fill can be found: contradiction.
                self._move_order(order["id"], "UNRECONCILED", error_text="position present without order or fill")
                self.health.set_condition(
                    "order_unreconciled", f"entry {order['cloid']} on {coin}: position present but order and fills not found",
                    level=PAUSE, auto_resume=False,
                )
                return "unreconciled"
        self._move_order(order["id"], "NOT_ACCEPTED", exchange_status="unknownOid")
        self.db.audit("order.resolved", {"cloid": order["cloid"], "status": "not_accepted"}, trade_id=order["trade_id"], order_id=order["id"])
        return "not_accepted"

    async def _resolve_entry(self, trade: Row) -> None:
        """Drive step for a trade still ``ENTERING``: only reached after a
        lost response or a restart."""
        trade_id = trade["id"]
        orders = self._orders(trade_id, "ENTRY")
        if not orders:
            self.db.set_trade_state(trade_id, "ABORTED", close_reason="no_entry_order", closed_at=self.clock.iso())
            return
        order = orders[-1]
        if order["state"] == "INTENT":
            self.db.set_order_state(order["id"], "SUBMITTING")
            order = self._order(order["id"])
            assert order is not None
        if order["state"] == "SUBMITTING":
            self._move_order(order["id"], "UNKNOWN", error_text="process restarted before the response was recorded")
            order = self._order(order["id"])
            assert order is not None
        if order["state"] == "UNRECONCILED":
            # Contradictory evidence was found earlier. Re-check the facts:
            # fills that have since arrived settle it one way, a flat
            # account settles it the other; otherwise the operator decides.
            if float(order["filled_size"] or 0.0) > self._tol(trade["coin"]):
                self.db.set_order_state(order["id"], "RESTING")
                self._refresh_order_from_fills(order["id"])
                self.health.clear_condition("order_unreconciled")
            else:
                try:
                    await self.sync_fills()
                    account = await self.adapter.get_account()
                except ExchangeError:
                    return
                if abs(account.szi(trade["coin"])) <= self._tol(trade["coin"]):
                    self.db.set_order_state(order["id"], "CANCELED", exchange_status="resolved_flat")
                    self.health.clear_condition("order_unreconciled")
                else:
                    return
            order = self._order(order["id"])
            assert order is not None
        if order["state"] == "UNKNOWN":
            result = await self._resolve_unknown(order)
            if result == "pending":
                created_ms = int(parse_iso(order["created_at"]).timestamp() * 1000)
                deadline = int(order["expires_after"] or (created_ms + self.s.entry_ttl_ms))
                if self.clock.now_ms() > deadline + RESOLVE_GRACE_MS + UNKNOWN_PAUSE_MS:
                    self.health.set_condition(
                        "order_unknown", f"entry order {order['cloid']} unresolved past its deadline", level=PAUSE, auto_resume=True
                    )
                return
            if result == "unreconciled":
                return
            self.health.clear_condition("order_unknown")
            if result == "not_accepted":
                await self._entry_failed(trade_id, ExchangeError(ErrorClass.UNKNOWN_OUTCOME, "request was not accepted"))
                return
        order = self._order(order["id"])
        assert order is not None
        if order["state"] in ("REJECTED", "NOT_ACCEPTED") or (order["state"] == "CANCELED" and not order["filled_size"]):
            await self._entry_failed(trade_id, ExchangeError(ErrorClass.NO_MATCH, order["error_text"] or order["state"]))
            return
        await self._confirm_entry(trade_id, None)

    # ================================================================== protection (PRD §10.5)
    def _sl_limit_price(self, coin: str, closing_is_buy: bool, trigger: float) -> float:
        """Aggressive limit attached to a stop-market trigger."""
        pct = self.s.sl_trigger_slippage_pct / 100.0
        raw = trigger * (1.0 + pct) if closing_is_buy else trigger * (1.0 - pct)
        return round_price(raw, self._szd(coin), "up" if closing_is_buy else "down")

    def _sl_covers(self, trade: Row, sl: Row, open_orders: Sequence[OpenOrder]) -> Optional[OpenOrder]:
        """§10.5 step 3: our SL cloid, a reduce-only stop trigger on the
        closing side at the intended price within one tick, covering the
        whole remaining position."""
        coin = trade["coin"]
        closing_is_buy = trade["side"] == "SELL"
        tick = tick_size(sl["trigger_price"], self._szd(coin))
        for oo in open_orders:
            if (
                oo.cloid == sl["cloid"]
                and oo.is_trigger
                and oo.reduce_only
                and oo.tpsl == "sl"
                and oo.is_buy == closing_is_buy
                and oo.trigger_px is not None
                and abs(oo.trigger_px - sl["trigger_price"]) <= tick + 1e-9
                and oo.size >= trade["remaining_size"] - self._tol(coin)
            ):
                return oo
        return None

    def _to_flatten(self, trade_id: int, reason: str) -> None:
        trade = self._trade(trade_id)
        if trade["state"] != "OPEN_UNPROTECTED":
            return
        self.db.set_trade_state(trade_id, "FLATTENING", close_reason=reason)
        self._since[(trade_id, "flatten")] = self.clock.monotonic()
        self.metrics.inc("flattens")
        if reason in ("protection_failure", "sl_invalid"):
            coin = trade["coin"]
            # §10.7: protection failure leading to a flatten pauses the asset until a manual resume.
            self.health.set_condition(
                f"protection_failure:{coin}", f"trade {trade_id} on {coin} could not be protected ({reason}); flattening",
                level=PAUSE, scope=coin, auto_resume=False,
            )
        elif reason != "kill":
            self.notifier.alert(f"Flattening trade {trade_id}", f"{trade['coin']}: {reason}", critical=False)

    async def _protect(self, trade: Row) -> None:
        trade_id, coin = trade["id"], trade["coin"]
        tol = self._tol(coin)
        since = self._since.setdefault((trade_id, "unprotected"), self.clock.monotonic())
        elapsed_ms = (self.clock.monotonic() - since) * 1000.0
        if trade["remaining_size"] <= tol:
            self._to_flatten(trade_id, trade["close_reason"] or "closed_before_protection")
            return
        if elapsed_ms >= self.s.protect_hard_ms:
            self._to_flatten(trade_id, "protection_failure")
            return

        closing_is_buy = trade["side"] == "SELL"
        sl = self._live_order(trade_id, "SL")
        if sl is not None and sl["state"] in ("INTENT", "SUBMITTING"):
            if sl["state"] == "INTENT":
                self.db.set_order_state(sl["id"], "SUBMITTING")
            self._move_order(sl["id"], "UNKNOWN", error_text="found unsent or unanswered during recovery")
            sl = self._order(sl["id"])
        if sl is not None and sl["state"] == "UNKNOWN":
            await self._resolve_unknown(sl)  # TC-052: re-place only if absent
            sl = self._live_order(trade_id, "SL")
        if sl is None:
            if self._sl_tries.get(trade_id, 0) >= 2:
                self._to_flatten(trade_id, "protection_failure")
                return
            self._sl_tries[trade_id] = self._sl_tries.get(trade_id, 0) + 1
            trigger = float(trade["sl_price"])
            sl = self._order_intent(
                trade, "SL", 0, is_buy=closing_is_buy, size=floor_size(trade["remaining_size"], self._szd(coin)),
                price=self._sl_limit_price(coin, closing_is_buy, trigger), kind="trigger", tif=None, trigger_px=trigger,
            )
            result = (await self._send(trade_id, [sl], Priority.PROTECT))[0]
            if result.status == "error" and result.error is not None:
                if result.error.kind == ErrorClass.INVALID_TPSL:
                    self._to_flatten(trade_id, "sl_invalid")  # §10.5 step 7: SL already through the market
                    return
                if result.error.kind == ErrorClass.LOCAL_BUG:
                    self._to_flatten(trade_id, "too_small")
                    return
                logger.warning("SL placement rejected for trade %s: %s", trade_id, result.error)
                return
            await self._ensure_tps(self._trade(trade_id))  # §10.5 step 2: SL, then the TP ladder
            sl = self._order(sl["id"])
            assert sl is not None
            if sl["state"] in ORDER_TERMINAL:
                return

        try:
            open_orders = await self.adapter.get_open_orders()
            self._rest_ok()
        except ExchangeError as exc:
            self._rest_failed("protect verify", exc)
            return
        trade = self._trade(trade_id)
        covering = self._sl_covers(trade, sl, open_orders)
        if covering is not None:
            if sl["state"] in ("SUBMITTING", "UNKNOWN"):
                self._move_order(sl["id"], "RESTING", oid=covering.oid, exchange_status="open")
            first_time = trade["protected_at"] is None
            self.db.set_trade_state(trade_id, "PROTECTED", protected_at=trade["protected_at"] or self.clock.iso())
            took_ms = (self.clock.monotonic() - since) * 1000.0
            self.metrics.observe("time_to_protected_ms", took_ms)
            self.db.audit("trade.protected", {"sl_cloid": sl["cloid"], "ms": round(took_ms), "size": covering.size}, trade_id=trade_id)
            self._since.pop((trade_id, "unprotected"), None)
            self._sl_tries.pop(trade_id, None)
            if self._tp_filled(trade_id) > tol:
                self.db.set_trade_state(trade_id, "REDUCING")
            await self._ensure_tps(self._trade(trade_id))
            if first_time:
                self._notify_placed(self._trade(trade_id))
            return
        if elapsed_ms >= self.s.protect_soft_ms and self._sl_tries.get(trade_id, 0) < 2:
            # §10.5 step 4: cancel the first SL by cloid and re-place once with a new cloid.
            logger.warning("SL for trade %s not verified after %.0f ms; re-placing", trade_id, elapsed_ms)
            await self._cancel_orders(trade, [sl], Priority.PROTECT)
            still = self.db.get("orders", sl["id"])
            if still and still["state"] not in ORDER_TERMINAL:
                # Could not confirm the cancel: stop tracking it; it is reduce-only and cleaned up on close.
                self._move_order(sl["id"], "CANCELED", exchange_status="replaced_unverified")

    def _tp_filled(self, trade_id: int) -> float:
        return float(self.db.scalar("SELECT COALESCE(SUM(size), 0) FROM fills WHERE trade_id = ? AND role = 'TP'", (trade_id,), default=0.0))

    async def _ensure_tps(self, trade: Row) -> None:
        """Place every TP leg that has no live order and is not yet
        filled. Non-critical (§10.5 step 9): a TP problem is logged and
        retried, and never causes a flatten."""
        trade_id, coin = trade["id"], trade["coin"]
        tol = self._tol(coin)
        if trade["remaining_size"] <= tol or trade["state"] not in ("OPEN_UNPROTECTED", "PROTECTED", "REDUCING"):
            return
        closing_is_buy = trade["side"] == "SELL"
        existing = self._orders(trade_id, "TP")
        live_total = sum(o["size"] - (o["filled_size"] or 0.0) for o in existing if o["state"] not in ORDER_TERMINAL)
        budget = trade["remaining_size"] - live_total  # never rest more TP size than the position
        intents: list[Row] = []
        for leg in loads(trade["tp_plan"], []):
            mine = [o for o in existing if o["leg"] == leg["level"]]
            if any(o["state"] not in ORDER_TERMINAL for o in mine):
                continue
            need = float(leg["size"]) - sum(float(o["filled_size"] or 0.0) for o in mine)
            need = floor_size(min(need, budget), self._szd(coin))
            if need <= tol or len(mine) >= MAX_TP_TRIES or need * float(leg["price"]) < self.s.risk.min_notional:
                continue
            budget -= need
            if self.s.tp_style == "trigger":
                intents.append(
                    self._order_intent(
                        trade, "TP", int(leg["level"]), is_buy=closing_is_buy, size=need,
                        price=self._sl_limit_price(coin, closing_is_buy, float(leg["price"])), kind="trigger", tif=None,
                        trigger_px=float(leg["price"]),
                    )
                )
            else:
                intents.append(
                    self._order_intent(trade, "TP", int(leg["level"]), is_buy=closing_is_buy, size=need, price=float(leg["price"]))
                )
        if not intents:
            return
        results = await self._send(trade_id, intents, Priority.PROTECT)
        for order, result in zip(intents, results, strict=True):
            if result.status == "error" and result.error is not None:
                logger.warning("TP leg %s for trade %s rejected: %s", order["leg"], trade_id, result.error)
                self.db.audit("tp.rejected", {"leg": order["leg"], "error": result.error.text}, trade_id=trade_id, order_id=order["id"])

    def _notify_placed(self, trade: Row) -> None:
        signal = self.db.get("signals", trade["signal_id"])
        assert signal is not None
        legs = [
            {"level": o["leg"], "size": o["size"], "price": o["trigger_price"] or o["price"]}
            for o in self._orders(trade["id"], "TP") if o["state"] not in ("REJECTED", "NOT_ACCEPTED")
        ]
        self.notifier.notify(
            "order_placed",
            message_id=signal["message_id"], channel=signal["channel_ref"], coin=trade["coin"], direction=trade["side"],
            size=trade["filled_size"], avg_entry=trade["avg_entry"], sl=trade["sl_price"], legs=legs,
            content_id=signal["content_id"], partial=trade["filled_size"] < trade["planned_size"] - self._tol(trade["coin"]),
        )

    # ================================================================== monitoring (PRD §10.1 stage 9)
    async def _sync_orders(self, trade: Row, view: ExchangeView) -> None:
        """Bring every live local order of a trade in line with the
        exchange (§12.5 rule 1: the exchange is the source of truth)."""
        by_cloid = view.open_by_cloid()
        need_fills = False
        for order in self._orders(trade["id"], live_only=True):
            if order["role"] == "ENTRY":
                continue
            if order["state"] in ("INTENT", "SUBMITTING"):
                if order["state"] == "INTENT":
                    self.db.set_order_state(order["id"], "SUBMITTING")
                self._move_order(order["id"], "UNKNOWN", error_text="found unsent or unanswered")
                order = self._order(order["id"])
                assert order is not None
            oo = by_cloid.get(order["cloid"])
            if oo is not None:
                fields: dict[str, Any] = {}
                if order["oid"] != oo.oid:
                    fields["oid"] = oo.oid
                if order["role"] == "SL":
                    if abs(oo.orig_size - order["size"]) > self._tol(order["coin"]):
                        fields["size"] = oo.orig_size
                    if oo.trigger_px is not None and oo.trigger_px != order["trigger_price"]:
                        fields["trigger_price"] = oo.trigger_px
                if order["state"] in ("UNKNOWN", "CANCEL_REQUESTED", "MODIFY_REQUESTED", "UNRECONCILED"):
                    self._move_order(order["id"], "RESTING", exchange_status="open", **fields)
                elif fields:
                    self.db.update("orders", order["id"], **fields)
                continue
            if order["state"] == "UNKNOWN":
                await self._resolve_unknown(order)
                continue
            try:
                status = await self.adapter.get_order(order["cloid"])
            except ExchangeError as exc:
                self._rest_failed("order status", exc)
                continue
            if not status.found:
                self._move_order(order["id"], "CANCELED", exchange_status="missing")
                self.db.audit("order.missing", {"cloid": order["cloid"], "role": order["role"]}, trade_id=trade["id"], order_id=order["id"])
                continue
            local = map_exchange_status(status.status or "")
            if local == "RESTING":
                continue  # the open-order list lags the status endpoint
            if local in ("FILLED", "TRIGGERED") or (status.filled_size or 0.0) > self._tol(order["coin"]):
                need_fills = True
            if local == "TRIGGERED":
                self._move_order(order["id"], "TRIGGERED", exchange_status=status.status)
            elif local == "CANCELED":
                self._move_order(order["id"], "CANCELED", exchange_status=status.status)
                if status.status in CANCEL_REASON_ALERT:
                    self.notifier.alert(
                        f"{order['role']} order cancelled by the exchange", f"{order['coin']} trade {trade['id']}: {status.status}",
                        critical=order["role"] == "SL",
                    )
                if status.status == "delistedCanceled":
                    self.health.set_condition(
                        f"asset_blocked:{order['coin']}", f"{order['coin']} delisted", level=BLOCK, scope=order["coin"], auto_resume=True
                    )
                if status.status == "liquidatedCanceled":
                    self.health.set_condition("liquidation", f"orders cancelled by liquidation on {order['coin']}", level=HALT, auto_resume=False)
            elif local == "REJECTED":
                self._move_order(order["id"], "REJECTED", exchange_status=status.status)
            elif local == "FILLED":
                self._move_order(order["id"], "FILLED", exchange_status=status.status, filled_size=order["size"])
        if need_fills:
            try:
                await self.sync_fills()
            except ExchangeError:
                pass

    def _foreign_fills(self, trade_id: int) -> list[Row]:
        acked = int(self.db.kv_get(f"foreign_ack:{trade_id}", 0) or 0)
        return self.db.all(
            "SELECT * FROM fills WHERE trade_id = ? AND role IS NULL AND tid > ? ORDER BY tid", (trade_id, acked)
        )

    def _mark_unreconciled(self, trade: Row, detail: str) -> None:
        coin = trade["coin"]
        self.db.set_trade_state(trade["id"], "UNRECONCILED")
        self.health.set_condition(
            f"recon_divergence:{coin}", f"trade {trade['id']} on {coin}: {detail}", level=BLOCK, scope=coin,
            auto_resume=False, alert=True,
        )
        self.metrics.inc("reconciliation_divergences")

    async def _monitor(self, trade: Row, view: Optional[ExchangeView] = None) -> None:
        trade_id, coin = trade["id"], trade["coin"]
        tol = self._tol(coin)
        if trade["remaining_size"] <= tol:
            self.db.set_trade_state(trade_id, "CLOSING")
            return
        if view is None:
            try:
                view = await self.view()
            except ExchangeError:
                return
        await self._sync_orders(trade, view)
        trade = self._trade(trade_id)
        if trade["remaining_size"] <= tol:
            self.db.set_trade_state(trade_id, "CLOSING")
            return

        # ---- position arithmetic (§12.5 rule 6)
        sign = 1.0 if trade["side"] == "BUY" else -1.0
        szi = view.account.szi(coin)
        if abs(szi - sign * trade["remaining_size"]) > 2 * tol:
            try:
                await self.sync_fills()  # the stream may have missed fills
                view = await self.view()
            except ExchangeError:
                return
            trade = self._trade(trade_id)
            szi = view.account.szi(coin)
            if trade["remaining_size"] <= tol or abs(szi) <= tol:
                if trade["remaining_size"] > tol:
                    self.db.audit("trade.closed_externally", {"exchange_szi": szi, "expected": sign * trade["remaining_size"]}, trade_id=trade_id)
                self.db.set_trade_state(trade_id, "CLOSING", close_reason=trade["close_reason"] or "external")
                return
            if abs(szi - sign * trade["remaining_size"]) > 2 * tol:
                self._mark_unreconciled(trade, f"exchange position {szi:g} differs from expected {sign * trade['remaining_size']:g}")
                return
        if self._foreign_fills(trade_id):
            self._mark_unreconciled(trade, "position changed by an order the bot did not place")
            return

        # ---- stop-loss coverage (FR-054, §10.5 step 8)
        sl = self._live_order(trade_id, "SL")
        covering = self._sl_covers(trade, sl, view.open_orders) if sl is not None else None
        if sl is not None and sl["state"] == "TRIGGERED":
            return  # the stop is firing; fills follow
        if covering is None:
            self.db.set_trade_state(trade_id, "OPEN_UNPROTECTED")
            self._since[(trade_id, "unprotected")] = self.clock.monotonic()
            self._sl_tries[trade_id] = 0
            self.notifier.alert(
                "Stop-loss missing or under-sized", f"trade {trade_id} on {coin}: re-placing now", critical=True
            )
            self.wake(trade_id)
            return
        if trade["state"] == "PROTECTED" and self._tp_filled(trade_id) > tol:
            self.db.set_trade_state(trade_id, "REDUCING")
        await self._maintain_sl(trade, sl, covering)  # type: ignore[arg-type]
        await self._ensure_tps(self._trade(trade_id))
        self._check_liquidation_proximity(trade, view)

    async def _maintain_sl(self, trade: Row, sl: Row, covering: OpenOrder) -> None:
        """Resize the SL to the remaining position after a TP fill (the
        IV-03 fallback) and, when enabled, move it to break-even after
        TP1 (FR-064)."""
        coin = trade["coin"]
        tol = self._tol(coin)
        want_size = floor_size(trade["remaining_size"], self._szd(coin)) if self.s.sl_resize_on_tp else covering.size
        want_trigger = float(sl["trigger_price"])
        if self.s.sl_breakeven_after_tp1 and trade["avg_entry"]:
            plan = loads(trade["tp_plan"], [])
            if plan:
                first = plan[0]
                done = self.db.scalar(
                    "SELECT COALESCE(SUM(f.size), 0) FROM fills f JOIN orders o ON o.id = f.order_id "
                    "WHERE o.trade_id = ? AND o.role = 'TP' AND o.leg = ?", (trade["id"], first["level"]), default=0.0,
                )
                if done >= float(first["size"]) - tol:
                    breakeven = round_price(float(trade["avg_entry"]), self._szd(coin), "nearest")
                    better = breakeven < want_trigger if trade["side"] == "SELL" else breakeven > want_trigger
                    if better:
                        want_trigger = breakeven
        tick = tick_size(want_trigger, self._szd(coin))
        if abs(covering.size - want_size) <= tol and abs((covering.trigger_px or 0.0) - want_trigger) <= tick / 2.0:
            return
        await self.modify_sl(trade["id"], trigger=want_trigger, size=want_size, reason="maintain")

    async def modify_sl(self, trade_id: int, *, trigger: Optional[float] = None, size: Optional[float] = None, reason: str = "operator") -> bool:
        """FR-063: modify the SL through the exchange modify action. On
        failure the order is left as it was and the operator is alerted;
        the caller's next verification pass re-checks coverage."""
        trade = self._trade(trade_id)
        sl = self._live_order(trade_id, "SL")
        if sl is None or sl["state"] != "RESTING":
            return False
        coin = trade["coin"]
        closing_is_buy = trade["side"] == "SELL"
        new_trigger = round_price(float(trigger if trigger is not None else sl["trigger_price"]), self._szd(coin), "nearest")
        new_size = floor_size(float(size if size is not None else sl["size"]), self._szd(coin))
        if new_size < trade["remaining_size"] - self._tol(coin):
            return False  # never shrink the stop below the position
        request = OrderRequest(
            coin=coin, is_buy=closing_is_buy, size=new_size, price=self._sl_limit_price(coin, closing_is_buy, new_trigger),
            reduce_only=True, cloid=sl["cloid"], kind="trigger", trigger_px=new_trigger, tpsl="sl",
        )
        self.db.set_order_state(sl["id"], "MODIFY_REQUESTED")
        try:
            result = await self.queue.submit(
                Priority.PROTECT, "modify", lambda nonce: self.adapter.modify(sl["cloid"], request, nonce),
                audit={"cloid": sl["cloid"], "size": new_size, "trigger": new_trigger, "reason": reason},
                trade_id=trade_id, order_id=sl["id"],
            )
        except OutcomeUnknown:
            self._move_order(sl["id"], "UNKNOWN", error_text="modify outcome unknown")
            return False
        except ExchangeError as exc:
            self._move_order(sl["id"], "RESTING")
            self.notifier.alert("SL modify failed; previous stop kept", f"trade {trade_id}: {exc.text}", critical=False)
            return False
        if result.status == "error":
            self._move_order(sl["id"], "RESTING")
            self.notifier.alert(
                "SL modify rejected; previous stop kept", f"trade {trade_id}: {result.error.text if result.error else ''}", critical=False
            )
            return False
        self._move_order(sl["id"], "RESTING", size=new_size, trigger_price=new_trigger, price=request.price, oid=result.oid or sl["oid"])
        if trigger is not None and reason == "operator":
            self.db.update("trades", trade_id, sl_price=new_trigger)
        self.wake(trade_id)
        return True

    def _check_liquidation_proximity(self, trade: Row, view: ExchangeView) -> None:
        """FR-074: alert once per crossing when mark is within a fraction
        of the distance from entry to liquidation."""
        fraction = self.s.liq_alert_fraction
        position = view.account.position(trade["coin"])
        context = self.contexts.get(trade["coin"])
        if not fraction or position is None or position.liquidation_px is None or not position.entry_px or context is None or not context.mark:
            return
        total = abs(position.entry_px - position.liquidation_px)
        left = abs(context.mark - position.liquidation_px)
        key = f"liq:{trade['id']}"
        if total > 0 and left <= fraction * total:
            self._alert_once(
                key, "Liquidation proximity", f"{trade['coin']}: mark {context.mark:g} is within {fraction:.0%} of liquidation at {position.liquidation_px:g}"
            )
        else:
            self._alerted.discard(key)

    # ================================================================== close, flatten, cleanup
    async def _cancel_orders(self, trade: Row, orders: Sequence[Row], priority: Priority = Priority.CANCEL) -> None:
        """Cancel by cloid. "Never placed, already cancelled, or filled"
        is benign: the order is re-read and its real state adopted."""
        targets: list[Row] = []
        for order in orders:
            current = self.db.get("orders", order["id"])
            assert current is not None
            if current["state"] in ORDER_TERMINAL or current["state"] in ("INTENT",):
                continue
            if current["state"] == "RESTING":
                self.db.set_order_state(current["id"], "CANCEL_REQUESTED")
            targets.append(current)
        if not targets:
            return
        cloids = [o["cloid"] for o in targets]
        try:
            results = await self.queue.submit(
                priority, "cancelByCloid", lambda nonce: self.adapter.cancel(trade["coin"], cloids, nonce),
                audit={"cloids": cloids}, trade_id=trade["id"], order_id=targets[0]["id"],
            )
        except ExchangeError as exc:
            logger.warning("cancel failed for trade %s: %s", trade["id"], exc)
            for order in targets:
                self._move_order(order["id"], "RESTING")
            return
        for order, error in zip(targets, results, strict=True):
            if error is None:
                self._move_order(order["id"], "CANCELED", exchange_status="canceled")
                continue
            if error.kind == ErrorClass.NOT_FOUND:
                try:
                    status = await self.adapter.get_order(order["cloid"])
                except ExchangeError:
                    continue
                if status.found and status.status and status.status != "open":
                    local = map_exchange_status(status.status)
                    if local == "FILLED":
                        try:
                            await self.sync_fills()
                        except ExchangeError:
                            pass
                    self._move_order(order["id"], "TRIGGERED" if local == "TRIGGERED" else local, exchange_status=status.status)
                elif not status.found:
                    self._move_order(order["id"], "CANCELED", exchange_status="missing")
            else:
                self._move_order(order["id"], "RESTING")

    async def _close_step(self, trade: Row) -> bool:
        """One attempt to bring the trade's position to zero with a
        reduce-only IOC at a bounded price. Returns True once flat."""
        trade_id, coin = trade["id"], trade["coin"]
        tol = self._tol(coin)
        if trade["state"] in ("FLATTENING", "HALTED_MANUAL"):
            # §10.5 step 5: cancel all trade orders, then close.
            await self._cancel_orders(trade, [o for o in self._orders(trade_id, live_only=True) if o["role"] in ("SL", "TP")], Priority.CLOSE)
        pending = self._live_order(trade_id, "CLOSE")
        if pending is not None:
            if pending["state"] in ("INTENT", "SUBMITTING"):
                if pending["state"] == "INTENT":
                    self.db.set_order_state(pending["id"], "SUBMITTING")
                self._move_order(pending["id"], "UNKNOWN", error_text="found unsent or unanswered")
                pending = self._order(pending["id"])
                assert pending is not None
            if pending["state"] == "UNKNOWN":
                await self._resolve_unknown(pending)
            elif pending["state"] == "PARTIALLY_FILLED":
                self._move_order(pending["id"], "CANCELED", exchange_status="iocCanceled")
        try:
            await self.sync_fills()
            account = await self.adapter.get_account()
            self._rest_ok()
        except ExchangeError as exc:
            self._rest_failed("close", exc)
            return False
        trade = self._trade(trade_id)
        closing_is_buy = trade["side"] == "SELL"
        szi = account.szi(coin)
        closable = (-szi if szi < 0 else 0.0) if closing_is_buy else (szi if szi > 0 else 0.0)
        size = floor_size(min(closable, trade["remaining_size"]), self._szd(coin))  # never more than the trade owns
        if size <= tol:
            return True
        mark = await self._mark(coin)
        if mark is None:
            return False
        tries = len(self._orders(trade_id, "CLOSE"))
        pct = min(self.s.flatten_max_slippage_pct, self.s.flatten_slippage_pct * (tries + 1)) / 100.0
        price = round_price(mark * (1.0 + pct) if closing_is_buy else mark * (1.0 - pct), self._szd(coin), "up" if closing_is_buy else "down")
        order = self._order_intent(trade, "CLOSE", 0, is_buy=closing_is_buy, size=size, price=price, kind="limit", tif="Ioc")
        result = (await self._send(trade_id, [order], Priority.CLOSE))[0]
        if result.status == "filled":
            try:
                await self.sync_fills()
            except ExchangeError:
                pass
            current = self.db.get("orders", order["id"])
            if current and current["state"] == "PARTIALLY_FILLED":
                self._move_order(order["id"], "CANCELED", exchange_status="iocCanceled")
        elif result.status == "error" and result.error is not None:
            logger.warning("close attempt %d for trade %s rejected: %s", tries + 1, trade_id, result.error)
        return False

    async def _flatten(self, trade: Row) -> None:
        trade_id = trade["id"]
        since = self._since.setdefault((trade_id, "flatten"), self.clock.monotonic())
        flat = await self._close_step(trade)
        trade = self._trade(trade_id)
        if flat:
            if trade["state"] == "FLATTENING":
                self.db.set_trade_state(trade_id, "CLOSING")
                self._since.pop((trade_id, "flatten"), None)
            await self._finish_close(self._trade(trade_id))
            return
        elapsed_ms = (self.clock.monotonic() - since) * 1000.0
        if trade["state"] == "FLATTENING" and elapsed_ms >= self.s.flatten_deadline_ms:
            # §10.5 step 6: flat not verified by the deadline.
            self.db.set_trade_state(trade_id, "HALTED_MANUAL")
            self.health.set_condition(
                "flatten_unverified", f"trade {trade_id} on {trade['coin']} could not be flattened; position may be UNPROTECTED",
                level=HALT, auto_resume=False,
            )

    async def _closing(self, trade: Row) -> None:
        trade_id = trade["id"]
        if trade["close_reason"] in BOT_CLOSE_REASONS and trade["remaining_size"] > self._tol(trade["coin"]):
            since = self._since.setdefault((trade_id, "close"), self.clock.monotonic())
            if not await self._close_step(trade):
                if (self.clock.monotonic() - since) * 1000.0 > 30000:
                    self._alert_once(
                        f"close:{trade_id}", "Close not complete after 30 s",
                        f"trade {trade_id} on {trade['coin']}: {self._trade(trade_id)['remaining_size']:g} still open; retrying",
                    )
                return
        await self._finish_close(self._trade(trade_id))

    async def _finish_close(self, trade: Row) -> None:
        """Position is zero: cancel leftovers, verify none remain,
        classify, account, notify (FR-055, FR-056, FR-075)."""
        trade_id, coin = trade["id"], trade["coin"]
        since = self._since.setdefault((trade_id, "cleanup"), self.clock.monotonic())
        await self._cancel_orders(trade, [o for o in self._orders(trade_id, live_only=True) if o["role"] != "ENTRY"])
        try:
            view = await self.view()
        except ExchangeError:
            return
        await self._sync_orders(trade, view)
        mine = {o["cloid"] for o in self._orders(trade_id)}
        leftovers = [o for o in view.open_orders if o.cloid in mine]
        still_live = [o for o in self._orders(trade_id, live_only=True) if o["role"] != "ENTRY" and o["state"] != "TRIGGERED"]
        if leftovers or still_live:
            if leftovers and not still_live:
                # Open on the exchange but terminal locally (a request that landed late): cancel by cloid.
                try:
                    await self.queue.submit(
                        Priority.CANCEL, "cancelByCloid",
                        lambda nonce: self.adapter.cancel(coin, [o.cloid for o in leftovers if o.cloid], nonce),
                        audit={"cloids": [o.cloid for o in leftovers], "reason": "leftover"}, trade_id=trade_id,
                    )
                except ExchangeError as exc:
                    logger.warning("leftover cancel failed for trade %s: %s", trade_id, exc)
            if (self.clock.monotonic() - since) > 60.0:
                self._alert_once(
                    f"leftover:{trade_id}", "Leftover order will not cancel",
                    f"trade {trade_id} on {coin}: {len(leftovers) or len(still_live)} reduce-only order(s) still open", critical=False,
                )
            return
        try:
            await self.sync_fills()
        except ExchangeError:
            return
        await self._attribute_unmatched_fills(trade)
        funding_total = await self._sync_funding(trade)
        trade = self._trade(trade_id)
        fills = self.db.all("SELECT * FROM fills WHERE trade_id = ? ORDER BY time, tid", (trade_id,))
        closing = [f for f in fills if f["role"] != "ENTRY" and f["side"] != trade["side"]]
        outcome = outcomes.classify_outcome(
            [{"role": f["role"], "liquidation": bool(f["liquidation"])} for f in closing], trade["close_reason"]
        )
        pnl = outcomes.trade_pnl(fills, funding_total)
        final = "CLOSED"
        self.db.set_trade_state(trade_id, final, outcome=outcome, closed_at=self.clock.iso(), **pnl)
        for key in [k for k in self._since if k[0] == trade_id]:
            self._since.pop(key, None)
        self.metrics.inc("trades_closed")
        self.metrics.inc(f"outcome_{outcome.lower()}")
        self._check_sl_slippage(trade, closing)
        signal = self.db.get("signals", trade["signal_id"])
        level_by_order = {o["id"]: o["leg"] for o in self._orders(trade_id)}
        self.notifier.notify(
            "signal_closed",
            outcome=outcome, content_id=signal["content_id"] if signal else None, coin=coin, direction=trade["side"],
            channel=signal["channel_ref"] if signal else None, trade_id=trade_id,
            legs=[
                {"role": f["role"], "level": level_by_order.get(f["order_id"]), "size": f["size"], "price": f["price"], "pnl": f["closed_pnl"] - f["fee"]}
                for f in closing
            ],
            realized_pnl=pnl["realized_pnl"], fees=pnl["fees"], funding=pnl["funding"], net_pnl=pnl["net_pnl"],
        )
        if outcome == outcomes.LIQUIDATED:
            self.health.set_condition("liquidation", f"trade {trade_id} on {coin} was liquidated", level=HALT, auto_resume=False)

    async def _attribute_unmatched_fills(self, trade: Row) -> None:
        """IV-14: when a stop triggers, the fills may carry an order id
        the bot never saw. If the exchange says our SL (or trigger TP)
        fired, closing fills with no order are attributed to it."""
        unmatched = self.db.all(
            "SELECT * FROM fills WHERE trade_id = ? AND role IS NULL AND side != ? AND liquidation = 0", (trade["id"], trade["side"])
        )
        if not unmatched:
            return
        for order in self._orders(trade["id"]):
            if order["role"] not in ("SL", "TP") or order["type"] != "trigger":
                continue
            fired = order["state"] in ("TRIGGERED", "FILLED") or order["exchange_status"] in ("triggered", "filled")
            if not fired:
                try:
                    status = await self.adapter.get_order(order["cloid"])
                except ExchangeError:
                    continue
                fired = bool(status.found and status.status in ("triggered", "filled"))
            if fired:
                with self.db.tx():
                    for fill in unmatched:
                        self.db.execute("UPDATE fills SET role = ?, order_id = ? WHERE tid = ?", (order["role"], order["id"], fill["tid"]))
                    self.db.audit(
                        "fills.attributed", {"tids": [f["tid"] for f in unmatched], "to": order["cloid"], "role": order["role"]},
                        trade_id=trade["id"], order_id=order["id"],
                    )
                return

    async def _sync_funding(self, trade: Row) -> float:
        """Record funding payments for the trade's holding period (FR-075)."""
        if not trade["opened_at"]:
            return 0.0
        opened_ms = int(parse_iso(trade["opened_at"]).timestamp() * 1000)
        try:
            payments = await self.adapter.get_funding(opened_ms)
        except ExchangeError as exc:
            logger.warning("funding history unavailable for trade %s: %s", trade["id"], exc)
            payments = []
        for payment in payments:
            if payment.coin != trade["coin"]:
                continue
            self.db.execute(
                "INSERT INTO funding_payments (time, coin, usdc, rate, szi, trade_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(time, coin) DO UPDATE SET trade_id = COALESCE(funding_payments.trade_id, excluded.trade_id)",
                (payment.time_ms, payment.coin, payment.usdc, payment.rate, payment.szi, trade["id"]),
            )
        return float(self.db.scalar("SELECT COALESCE(SUM(usdc), 0) FROM funding_payments WHERE trade_id = ?", (trade["id"],), default=0.0))

    def _check_sl_slippage(self, trade: Row, closing: Sequence[Row]) -> None:
        """RC-09: alert when the stop filled further from its trigger than
        the allowance used in sizing."""
        sl_fills = [f for f in closing if f["role"] == "SL"]
        if not sl_fills:
            return
        size = sum(f["size"] for f in sl_fills)
        avg = sum(f["size"] * f["price"] for f in sl_fills) / size
        slip = avg - trade["sl_price"] if trade["side"] == "SELL" else trade["sl_price"] - avg
        self.metrics.observe("sl_slippage_points", slip)
        if slip > self.s.risk.sl_slippage_allowance_points + 1e-9:
            self.notifier.alert(
                "Stop-loss slippage exceeded allowance",
                f"trade {trade['id']} on {trade['coin']}: filled {avg:g} vs trigger {trade['sl_price']:g} "
                f"({slip:g} > {self.s.risk.sl_slippage_allowance_points:g} points)", critical=False,
            )

    async def _hold_unreconciled(self, trade: Row, view: Optional[ExchangeView] = None) -> None:
        """Keep the SL covering the *actual* position while the operator
        decides (§10.1 stage 9, §14)."""
        trade_id, coin = trade["id"], trade["coin"]
        if view is None:
            try:
                view = await self.view()
            except ExchangeError:
                return
        await self._sync_orders(trade, view)
        sign = 1.0 if trade["side"] == "BUY" else -1.0
        actual = view.account.szi(coin) * sign
        if actual <= self._tol(coin):
            self.db.set_trade_state(trade_id, "CLOSING", close_reason=trade["close_reason"] or "external")
            self.wake(trade_id)
            return
        sl = self._live_order(trade_id, "SL")
        target = floor_size(actual, self._szd(coin))
        if sl is not None and sl["state"] == "RESTING" and abs(sl["size"] - target) > self._tol(coin):
            self.db.update("trades", trade_id, remaining_size=target)
            await self.modify_sl(trade_id, size=target, reason="unreconciled")
            self._recompute_trade(trade_id)

    # ================================================================== drive loop
    def wake(self, trade_id: int) -> None:
        event = self._wake.get(trade_id)
        if event is not None:
            event.set()

    def _ensure_loop(self, trade_id: int) -> None:
        task = self._loops.get(trade_id)
        if task is None or task.done():
            self._wake.setdefault(trade_id, asyncio.Event())
            self._loops[trade_id] = asyncio.get_running_loop().create_task(self._trade_loop(trade_id), name=f"trade-{trade_id}")

    def _interval(self, state: str) -> Optional[float]:
        if state in FAST_STATES:
            return self.fast_poll_s
        if state == "HALTED_MANUAL":
            return self.halted_poll_s
        return None  # steady states are driven by fills and the reconciler

    async def _trade_loop(self, trade_id: int) -> None:
        event = self._wake.setdefault(trade_id, asyncio.Event())
        try:
            while True:
                event.clear()
                try:
                    await self.drive(trade_id)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the loop must survive to keep protecting the position
                    logger.exception("drive failed for trade %s", trade_id)
                    self.metrics.inc("drive_errors")
                trade = self.db.get("trades", trade_id)
                if trade is None or trade["state"] in TRADE_TERMINAL:
                    return
                await wait_event(event, self._interval(trade["state"]))
        finally:
            self._loops.pop(trade_id, None)

    async def drive(self, trade_id: int, view: Optional[ExchangeView] = None) -> None:
        """Advance one trade by one step. Idempotent and serialised per trade."""
        lock = self._locks.setdefault(trade_id, asyncio.Lock())
        async with lock:
            trade = self.db.get("trades", trade_id)
            if trade is None or trade["state"] in TRADE_TERMINAL:
                return
            state = trade["state"]
            if state == "ENTERING":
                await self._resolve_entry(trade)
            elif state == "OPEN_UNPROTECTED":
                await self._protect(trade)
            elif state in ("PROTECTED", "REDUCING"):
                await self._monitor(trade, view)
            elif state in ("FLATTENING", "HALTED_MANUAL"):
                await self._flatten(trade)
            elif state == "CLOSING":
                await self._closing(trade)
            elif state == "UNRECONCILED":
                await self._hold_unreconciled(trade, view)
            after = self.db.get("trades", trade_id)
            if after and after["state"] != state and after["state"] not in TRADE_TERMINAL:
                self.wake(trade_id)

    async def drive_all(self, view: Optional[ExchangeView] = None) -> None:
        for trade in self.open_trades():
            self._ensure_loop(trade["id"])
            if trade["state"] in ("PROTECTED", "REDUCING", "UNRECONCILED"):
                try:
                    await self.drive(trade["id"], view)
                except Exception:  # noqa: BLE001
                    logger.exception("drive failed for trade %s", trade["id"])
            else:
                self.wake(trade["id"])

    async def stop(self) -> None:
        for task in list(self._loops.values()):
            task.cancel()
        for task in list(self._loops.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loops.clear()

    # ================================================================== operator commands
    def request_close(self, trade_id: int, reason: str = "manual") -> bool:
        """FR-062 / FR-044: close a bot trade reduce-only."""
        trade = self.db.get("trades", trade_id)
        if trade is None or trade["state"] in TRADE_TERMINAL:
            return False
        state = trade["state"]
        if state in ("PROTECTED", "REDUCING", "UNRECONCILED"):
            self.db.set_trade_state(trade_id, "CLOSING", close_reason=reason)
            if state == "UNRECONCILED":
                self.health.clear_condition(f"recon_divergence:{trade['coin']}")
        elif state == "OPEN_UNPROTECTED":
            self._to_flatten(trade_id, reason)
        elif state in ("FLATTENING", "CLOSING", "HALTED_MANUAL", "ENTERING"):
            pass  # already closing, or resolved by the entry path (kill flag)
        self.db.audit("trade.close_requested", {"reason": reason, "state": state}, trade_id=trade_id)
        self._ensure_loop(trade_id)
        self.wake(trade_id)
        return True

    def abort_entry(self, trade_id: int) -> bool:
        """``botctl reconcile --close`` on a trade stuck ``ENTERING`` with
        an unreconciled entry order: the operator confirms the bot does
        not own the position. The position, if any, is then foreign."""
        trade = self.db.get("trades", trade_id)
        if trade is None or trade["state"] != "ENTERING":
            return False
        for order in self._orders(trade_id, "ENTRY", live_only=True):
            if order["state"] == "UNRECONCILED":
                self.db.set_order_state(order["id"], "CANCELED", exchange_status="operator_abort")
            else:
                return False
        self.db.set_trade_state(trade_id, "ABORTED", close_reason="operator_abort", closed_at=self.clock.iso())
        signal = self.db.get("signals", trade["signal_id"])
        if signal and signal["state"] == "ENTERING":
            self._skip_signal(signal, "FAILED", "entry aborted by the operator after an unreconciled outcome")
        self.health.clear_condition("order_unreconciled")
        return True

    async def kill(self, by: str = "operator", timeout_s: float = 30.0) -> dict[str, Any]:
        """FR-044: pause, close every bot position reduce-only, cancel
        leftovers, verify flat, stay paused. Foreign positions and
        orders are never touched (FR-060)."""
        self._kill = True
        self.health.pause("kill switch", by)
        targets = [t["id"] for t in self.open_trades()]
        for trade_id in targets:
            self.request_close(trade_id, "kill")
        deadline = self.clock.monotonic() + timeout_s
        while self.clock.monotonic() < deadline:
            if not [t for t in self.open_trades() if t["id"] in targets]:
                break
            await asyncio.sleep(min(0.2, self.fast_poll_s))
        remaining = [t for t in self.open_trades() if t["id"] in targets]
        self._kill = False
        left = [{"trade_id": t["id"], "coin": t["coin"], "state": t["state"], "size": t["remaining_size"]} for t in remaining]
        report: dict[str, Any] = {"closed": len(targets) - len(remaining), "remaining": left}
        self.db.audit("control.kill", {"by": by, **report})
        if remaining:
            self.notifier.alert(
                "KILL incomplete", "; ".join(f"trade {r['trade_id']} {r['coin']} {r['state']} size {r['size']:g}" for r in left), critical=True,
            )
        else:
            self.notifier.alert("Kill switch: flat and paused", f"{report['closed']} trade(s) closed", critical=False)
        return report

    async def adopt(self, trade_id: int) -> bool:
        """``botctl reconcile --adopt``: accept the exchange position as
        the trade's size and resume managing it."""
        trade = self.db.get("trades", trade_id)
        if trade is None or trade["state"] != "UNRECONCILED":
            return False
        try:
            await self.sync_fills()
            view = await self.view()
        except ExchangeError:
            return False
        trade = self._trade(trade_id)
        sign = 1.0 if trade["side"] == "BUY" else -1.0
        actual = view.account.szi(trade["coin"]) * sign
        foreign = self._foreign_fills(trade_id)
        if foreign:
            self.db.kv_set(f"foreign_ack:{trade_id}", max(f["tid"] for f in foreign))
        drift = actual - trade["remaining_size"]
        if abs(drift) > self._tol(trade["coin"]):
            self.db.kv_set(f"trade_adjust:{trade_id}", float(self.db.kv_get(f"trade_adjust:{trade_id}", 0.0) or 0.0) + drift)
        self._recompute_trade(trade_id)
        self.db.audit("trade.adopted", {"exchange_size": actual}, trade_id=trade_id)
        self.db.set_trade_state(trade_id, "PROTECTED")
        self.health.clear_condition(f"recon_divergence:{trade['coin']}")
        self._ensure_loop(trade_id)
        await self.drive(trade_id)
        return True
