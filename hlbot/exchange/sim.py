"""Simulated exchange.

One implementation serves two purposes:

* **paper mode** (FR-080): fills are simulated against live market data
  fed in by ``PaperAdapter``;
* **component tests** (PRD §16.1 "fake adapter"): the same object with a
  scriptable ``Faults`` plan that drops responses, rejects orders, hides
  orders from the open-order list, and so on.

It models the Hyperliquid behaviours the engine depends on: one net
position per coin, IOC limit entries, reduce-only resting limits,
mark-triggered stop/take-profit orders, the 10 USDC minimum, tick and
size validation, and fills carrying ``oid`` / ``cloid`` / ``closedPnl``.

Simulated results are not execution results: there is no queue
position, no book depth, no funding, and a fill always happens at the
touch. Assumptions made where the official docs are silent are marked
``IV-nn`` and must be proven on testnet before they are relied on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from hlbot.domain.risk import estimate_liquidation_price
from hlbot.domain.rounding import MIN_ORDER_NOTIONAL, floor_size, is_valid_price, is_valid_size
from hlbot.exchange.base import (
    AccountState,
    Candle,
    DexAccount,
    ErrorClass,
    ExchangeError,
    Fill,
    FundingPayment,
    Instrument,
    Limits,
    MarketContext,
    OpenOrder,
    OrderRequest,
    OrderStatus,
    OrderUpdate,
    OutcomeUnknown,
    PlaceResult,
    Position,
    Quote,
    SignerInfo,
    StreamHandlers,
    StreamHealth,
    dex_of,
)
from hlbot.exchange.errors import to_error
from hlbot.timeutil import Clock

ERR_NO_MATCH = "Order could not immediately match against any resting orders."
ERR_TICK = "Price must be divisible by tick size."
ERR_MIN_VALUE = "Order must have minimum value of $10."
ERR_SIZE = "Order has invalid size."
ERR_MARGIN = "Insufficient margin to place order."
ERR_REDUCE_ONLY = "Reduce only order would increase position."
ERR_TPSL = "Invalid TP/SL price."
ERR_NOT_FOUND = "Order was never placed, already canceled, or filled."
ERR_OI_CAP = "Order would exceed open interest cap."
ERR_RATE = "Too many requests: rate limit exceeded (429)."


@dataclass
class Faults:
    """Scriptable failures for tests. Everything is off by default."""

    # Called per order in place(); return None for normal handling or one of:
    #   "lose"          request never reaches the exchange; caller sees a timeout
    #   "drop"          request is processed; caller sees a timeout
    #   "reject:<text>" this order is rejected with <text>
    #   "partial:<f>"   an IOC fills only fraction <f> of its size
    on_place: Optional[Callable[[OrderRequest], Optional[str]]] = None
    hide_open_order: Optional[Callable[[OpenOrder], bool]] = None
    hide_status_cloids: set[str] = field(default_factory=set)
    rest_down: bool = False
    rate_limited_actions: int = 0
    cancel_text: Optional[str] = None
    oi_capped: set[str] = field(default_factory=set)
    signer_authorized: bool = True
    signer_expires_at_ms: Optional[int] = None
    min_notional_on_reduce_only: bool = True  # IV-07: assumed to apply
    # IV-14 unfavourable case: when a trigger fires, its fills carry a new
    # order id and no client order id.
    anonymous_trigger_fills: bool = False


@dataclass
class _Order:
    oid: int
    req: OrderRequest
    status: str  # exchange status string
    remaining: float
    orig: float
    created_ms: int
    status_ms: int


class SimExchange:
    name = "sim"

    def __init__(
        self,
        instruments: Optional[dict[str, Instrument]] = None,
        *,
        equity: float = 10000.0,
        taker_fee: float = 0.00045,
        maker_fee: float = 0.00015,
        clock: Optional[Clock] = None,
        default_leverage: int = 20,
    ):
        self.instruments: dict[str, Instrument] = dict(instruments or {})
        self.clock = clock or Clock()
        self.balance = float(equity)
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.default_leverage = default_leverage
        self.faults = Faults()
        self.quotes: dict[str, Quote] = {}
        self.contexts: dict[str, MarketContext] = {}
        self.candles: dict[tuple[str, str], list[Candle]] = {}  # (coin, interval) -> candles, oldest first
        self.candle_requests = 0
        self.positions: dict[str, list[float]] = {}  # coin -> [szi, entry_px]
        self.leverage: dict[str, int] = {}
        self.orders: dict[int, _Order] = {}
        self.by_cloid: dict[str, int] = {}
        self.fills: list[Fill] = []
        self.requests: list[dict[str, Any]] = []  # every signed action received, for assertions
        self.on_change: Optional[Callable[[], None]] = None
        self._next_oid = 1000
        self._next_tid = 1
        self._handlers = StreamHandlers()
        self._events: asyncio.Queue = asyncio.Queue()
        self._pump: Optional[asyncio.Task] = None
        self._health = StreamHealth(connected=True)
        self._last_nonce = 0

    # ------------------------------------------------------------ persistence (paper mode)
    def snapshot(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "positions": self.positions,
            "leverage": self.leverage,
            "next_oid": self._next_oid,
            "next_tid": self._next_tid,
            "orders": [
                {
                    "oid": o.oid,
                    "req": o.req.__dict__,
                    "status": o.status,
                    "remaining": o.remaining,
                    "orig": o.orig,
                    "created_ms": o.created_ms,
                    "status_ms": o.status_ms,
                }
                for o in self.orders.values()
            ],
            "fills": [f.__dict__ for f in self.fills[-500:]],
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.balance = float(data.get("balance", self.balance))
        self.positions = {k: [float(v[0]), float(v[1])] for k, v in (data.get("positions") or {}).items()}
        self.leverage = {k: int(v) for k, v in (data.get("leverage") or {}).items()}
        self._next_oid = int(data.get("next_oid", self._next_oid))
        self._next_tid = int(data.get("next_tid", self._next_tid))
        self.orders = {}
        self.by_cloid = {}
        for raw in data.get("orders") or []:
            order = _Order(
                oid=int(raw["oid"]),
                req=OrderRequest(**raw["req"]),
                status=raw["status"],
                remaining=float(raw["remaining"]),
                orig=float(raw["orig"]),
                created_ms=int(raw["created_ms"]),
                status_ms=int(raw["status_ms"]),
            )
            self.orders[order.oid] = order
            self.by_cloid[order.req.cloid] = order.oid
        self.fills = [Fill(**raw) for raw in data.get("fills") or []]

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    # ------------------------------------------------------------ event delivery
    def _emit(self, kind: str, payload: Any) -> None:
        self._events.put_nowait((kind, payload))
        if self._pump is None or self._pump.done():
            self._pump = asyncio.get_running_loop().create_task(self._deliver())

    async def _deliver(self) -> None:
        while not self._events.empty():
            kind, payload = self._events.get_nowait()
            handler = getattr(self._handlers, kind, None)
            if handler is None:
                continue
            if kind == "on_fill":
                await handler(payload, False)
            else:
                await handler(payload)

    async def drain(self) -> None:
        """Wait until every queued stream event has been delivered."""
        while (self._pump and not self._pump.done()) or not self._events.empty():
            if self._pump and not self._pump.done():
                await self._pump
            else:
                await asyncio.sleep(0)

    # ------------------------------------------------------------ market data
    def add_instrument(self, coin: str, sz_decimals: int = 4, max_leverage: int = 25, asset_id: int = 110003) -> None:
        self.instruments[coin] = Instrument(coin, asset_id, sz_decimals, max_leverage, dex_of(coin))

    async def set_quote(
        self,
        coin: str,
        bid: Optional[float],
        ask: Optional[float],
        mark: Optional[float] = None,
        oracle: Optional[float] = None,
        *,
        emit: bool = True,
    ) -> None:
        """Move the market. Matches resting orders, then (optionally)
        delivers the quote to the engine like a WebSocket update would."""
        now = self.clock.now_ms()
        if mark is None:
            mark = (bid + ask) / 2.0 if bid is not None and ask is not None else (bid or ask)
        quote = Quote(coin, bid, ask, None, None, now, now)
        context = MarketContext(coin, mark, oracle if oracle is not None else mark, mark, 0.0, None, now)
        self.quotes[coin] = quote
        self.contexts[coin] = context
        self._health.last_message_ms = now
        self._health.quote_ms[coin] = now
        self._match(coin)
        if emit:
            self._emit("on_context", context)
            self._emit("on_quote", quote)
            await self.drain()

    def touch(self) -> None:
        """Refresh quote timestamps without moving prices (a quiet market)."""
        now = self.clock.now_ms()
        self.quotes = {c: replace(q, exchange_ms=now, received_ms=now) for c, q in self.quotes.items()}
        self.contexts = {c: replace(x, received_ms=now) for c, x in self.contexts.items()}
        self._health.last_message_ms = now

    def _check_rest(self) -> None:
        if self.faults.rest_down:
            raise OutcomeUnknown("simulated REST outage")

    async def get_instruments(self) -> dict[str, Instrument]:
        self._check_rest()
        return dict(self.instruments)

    async def get_market(self, coin: str) -> MarketContext:
        self._check_rest()
        context = self.contexts.get(coin)
        if context is None:
            raise ExchangeError(ErrorClass.OTHER, f"no market data for {coin}")
        return replace(context, received_ms=self.clock.now_ms())

    async def get_candles(self, coin: str, interval: str, count: int) -> list[Candle]:
        self._check_rest()
        self.candle_requests += 1
        return list(self.candles.get((coin, interval), []))[-count:]

    async def get_book(self, coin: str) -> Quote:
        self._check_rest()
        quote = self.quotes.get(coin)
        if quote is None:
            raise ExchangeError(ErrorClass.OTHER, f"no book for {coin}")
        now = self.clock.now_ms()
        return replace(quote, exchange_ms=now, received_ms=now)

    async def get_blocked_coins(self) -> list[str]:
        self._check_rest()
        return sorted(self.faults.oi_capped)

    # ------------------------------------------------------------ account
    def _mark(self, coin: str) -> Optional[float]:
        context = self.contexts.get(coin)
        return context.mark if context else None

    def _szi(self, coin: str) -> float:
        return self.positions.get(coin, [0.0, 0.0])[0]

    def _unrealized(self, coin: str) -> float:
        szi, entry = self.positions.get(coin, [0.0, 0.0])
        mark = self._mark(coin)
        return szi * (mark - entry) if szi and mark is not None else 0.0

    def equity(self) -> float:
        return self.balance + sum(self._unrealized(coin) for coin in self.positions)

    async def get_account(self) -> AccountState:
        self._check_rest()
        now = self.clock.now_ms()
        dexes = {inst.dex for inst in self.instruments.values()} or {""}
        equity = self.equity()
        positions: dict[str, Position] = {}
        margin: dict[str, float] = {d: 0.0 for d in dexes}
        notional: dict[str, float] = {d: 0.0 for d in dexes}
        maintenance: dict[str, float] = {d: 0.0 for d in dexes}
        for coin, (szi, entry) in self.positions.items():
            if abs(szi) < 1e-12:
                continue
            inst = self.instruments.get(coin)
            mark = self._mark(coin) or entry
            lev = self.leverage.get(coin, self.default_leverage)
            ntl = abs(szi) * mark
            dex = inst.dex if inst else dex_of(coin)
            margin[dex] = margin.get(dex, 0.0) + ntl / lev
            notional[dex] = notional.get(dex, 0.0) + ntl
            max_lev = inst.max_leverage if inst else lev
            maintenance[dex] = maintenance.get(dex, 0.0) + ntl / (2.0 * max_lev)
            liq = estimate_liquidation_price("BUY" if szi > 0 else "SELL", entry, abs(szi), max_lev, equity=equity)
            positions[coin] = Position(
                coin=coin,
                szi=szi,
                entry_px=entry,
                unrealized_pnl=self._unrealized(coin),
                leverage=float(lev),
                leverage_type="cross",
                liquidation_px=liq,
                margin_used=ntl / lev,
                cum_funding=0.0,
            )
        by_dex = {
            d: DexAccount(d, equity, margin.get(d, 0.0), notional.get(d, 0.0), max(0.0, equity - margin.get(d, 0.0)), maintenance.get(d, 0.0))
            for d in dexes
        }
        return AccountState(by_dex, positions, now, now)

    def _open_order(self, order: _Order) -> OpenOrder:
        req = order.req
        return OpenOrder(
            coin=req.coin,
            oid=order.oid,
            cloid=req.cloid,
            is_buy=req.is_buy,
            price=req.price,
            size=order.remaining,
            orig_size=order.orig,
            reduce_only=req.reduce_only,
            is_trigger=req.kind == "trigger",
            trigger_px=req.trigger_px,
            tpsl=req.tpsl if req.kind == "trigger" else None,
            is_position_tpsl=False,
            order_type=(("Stop" if req.tpsl == "sl" else "Take Profit") + (" Market" if req.is_market else " Limit"))
            if req.kind == "trigger"
            else "Limit",
            timestamp=order.created_ms,
        )

    def open_orders(self) -> list[OpenOrder]:
        return [self._open_order(o) for o in self.orders.values() if o.status == "open"]

    async def get_open_orders(self) -> list[OpenOrder]:
        self._check_rest()
        orders = self.open_orders()
        if self.faults.hide_open_order:
            orders = [o for o in orders if not self.faults.hide_open_order(o)]
        return orders

    async def get_order(self, cloid: str) -> OrderStatus:
        self._check_rest()
        oid = self.by_cloid.get(cloid)
        if oid is None or cloid in self.faults.hide_status_cloids:
            return OrderStatus(found=False)
        order = self.orders[oid]
        return OrderStatus(True, order.status, order.oid, cloid, order.req.coin, order.remaining, order.orig, order.status_ms)

    async def get_fills(self, since_ms: int) -> list[Fill]:
        self._check_rest()
        return [f for f in self.fills if f.time_ms >= since_ms]

    async def get_funding(self, since_ms: int) -> list[FundingPayment]:
        self._check_rest()
        return []

    async def get_limits(self) -> Limits:
        self._check_rest()
        return Limits(len(self.requests), 10000, self.taker_fee, self.maker_fee)

    async def get_leverage(self, coin: str) -> Optional[int]:
        self._check_rest()
        return self.leverage.get(coin, self.default_leverage)

    async def verify_signer(self) -> SignerInfo:
        self._check_rest()
        return SignerInfo(self.faults.signer_authorized, "sim", self.faults.signer_expires_at_ms, "simulated")

    # ------------------------------------------------------------ signed actions
    def _action(self, kind: str, nonce: int, detail: Any) -> None:
        if self.faults.rate_limited_actions > 0:
            self.faults.rate_limited_actions -= 1
            raise ExchangeError(ErrorClass.RATE_LIMIT, ERR_RATE)
        if nonce <= self._last_nonce:
            raise ExchangeError(ErrorClass.OTHER, f"nonce {nonce} is not greater than {self._last_nonce}")
        self._last_nonce = nonce
        self.requests.append({"kind": kind, "nonce": nonce, "detail": detail})

    async def set_leverage(self, coin: str, leverage: int, cross: bool, nonce: int) -> None:
        self._action("leverage", nonce, {"coin": coin, "leverage": leverage, "cross": cross})
        inst = self.instruments.get(coin)
        if inst and leverage > inst.max_leverage:
            raise ExchangeError(ErrorClass.OTHER, "Invalid leverage")
        self.leverage[coin] = int(leverage)
        self._changed()

    async def place(
        self, orders: Sequence[OrderRequest], nonce: int, expires_after: Optional[int] = None
    ) -> list[PlaceResult]:
        directives = [self.faults.on_place(o) if self.faults.on_place else None for o in orders]
        if "lose" in directives:
            raise OutcomeUnknown("simulated timeout: request lost")
        self._action("order", nonce, [o.cloid for o in orders])
        if expires_after is not None and self.clock.now_ms() > expires_after:
            raise ExchangeError(ErrorClass.EXPIRED, "Order expired: expiresAfter has passed")
        results = [self._place_one(o, d) for o, d in zip(orders, directives, strict=True)]
        self._changed()
        if "drop" in directives:
            raise OutcomeUnknown("simulated timeout: response lost")
        return results

    def _error(self, text: str) -> PlaceResult:
        return PlaceResult("error", error=to_error(text))

    def _validate(self, req: OrderRequest, inst: Optional[Instrument]) -> Optional[str]:
        if inst is None or inst.is_delisted:
            return f"Unknown or delisted asset {req.coin}"
        if req.cloid in self.by_cloid:
            return "Duplicate client order id."  # IV-06: assumed rejected
        if not is_valid_price(req.price, inst.sz_decimals):
            return ERR_TICK
        if req.kind == "trigger" and (req.trigger_px is None or not is_valid_price(req.trigger_px, inst.sz_decimals)):
            return ERR_TICK
        if not is_valid_size(req.size, inst.sz_decimals):
            return ERR_SIZE
        reference = req.trigger_px if req.kind == "trigger" and req.trigger_px else req.price
        if req.size * reference < MIN_ORDER_NOTIONAL - 1e-9 and (
            not req.reduce_only or self.faults.min_notional_on_reduce_only
        ):
            return ERR_MIN_VALUE
        if req.kind == "trigger" and not req.reduce_only:
            return "TP/SL orders must be reduce only."
        return None

    def _closable(self, coin: str, is_buy: bool) -> float:
        """Size a reduce-only order on this side could close."""
        szi = self._szi(coin)
        if is_buy and szi < 0:
            return -szi
        if not is_buy and szi > 0:
            return szi
        return 0.0

    def _place_one(self, req: OrderRequest, directive: Optional[str]) -> PlaceResult:
        inst = self.instruments.get(req.coin)
        problem = self._validate(req, inst)
        if directive and directive.startswith("reject:"):
            problem = directive.split(":", 1)[1]
        if problem:
            return self._error(problem)
        assert inst is not None
        now = self.clock.now_ms()
        if req.reduce_only and self._closable(req.coin, req.is_buy) <= 1e-12:
            return self._error(ERR_REDUCE_ONLY)
        if not req.reduce_only:
            if req.coin in self.faults.oi_capped:
                return self._error(ERR_OI_CAP)
            lev = self.leverage.get(req.coin, self.default_leverage)
            used = sum(abs(s) * (self._mark(c) or e) / self.leverage.get(c, self.default_leverage) for c, (s, e) in self.positions.items())
            if req.size * req.price / lev > self.equity() - used + 1e-9:
                return self._error(ERR_MARGIN)

        quote = self.quotes.get(req.coin)
        mark = self._mark(req.coin)

        if req.kind == "trigger":
            if mark is None:
                return self._error("No mark price available.")
            assert req.trigger_px is not None
            closing_long = not req.is_buy
            if req.tpsl == "sl":
                invalid = req.trigger_px >= mark if closing_long else req.trigger_px <= mark
            else:
                invalid = req.trigger_px <= mark if closing_long else req.trigger_px >= mark
            if invalid:
                return self._error(ERR_TPSL)
            order = self._register(req, "open", now)
            self._emit_update(order)
            return PlaceResult("resting", oid=order.oid)

        touch = (quote.ask if req.is_buy else quote.bid) if quote else None
        marketable = touch is not None and (touch <= req.price if req.is_buy else touch >= req.price)

        if req.tif == "Ioc":
            if not marketable:
                return self._error(ERR_NO_MATCH)
            size = req.size
            if directive and directive.startswith("partial:"):
                size = floor_size(req.size * float(directive.split(":", 1)[1]), inst.sz_decimals)
            if req.reduce_only:
                size = min(size, self._closable(req.coin, req.is_buy))
            if size <= 0:
                return self._error(ERR_NO_MATCH)
            order = self._register(req, "open", now)
            assert touch is not None
            self._fill(order, touch, size, taker=True)
            if order.remaining > 1e-12:
                order.status = "canceled"  # IV-08: unfilled IOC remainder is cancelled
                order.status_ms = self.clock.now_ms()
                self._emit_update(order)
            return PlaceResult("filled", oid=order.oid, total_size=size, avg_price=touch)

        order = self._register(req, "open", now)
        if marketable:
            assert touch is not None
            self._fill(order, touch, min(order.remaining, self._closable(req.coin, req.is_buy)) if req.reduce_only else order.remaining, taker=True)
            if order.status == "filled":
                return PlaceResult("filled", oid=order.oid, total_size=order.orig, avg_price=touch)
        self._emit_update(order)
        return PlaceResult("resting", oid=order.oid)

    def _register(self, req: OrderRequest, status: str, now: int) -> _Order:
        order = _Order(self._next_oid, req, status, req.size, req.size, now, now)
        self._next_oid += 1
        self.orders[order.oid] = order
        self.by_cloid[req.cloid] = order.oid
        return order

    def _emit_update(self, order: _Order) -> None:
        self._emit(
            "on_order_update",
            OrderUpdate(order.oid, order.req.cloid, order.req.coin, order.status, order.status_ms, order.remaining, order.orig),
        )

    def _fill(self, order: _Order, price: float, size: float, *, taker: bool, liquidation: bool = False) -> None:
        req = order.req
        inst = self.instruments[req.coin]
        size = floor_size(size, inst.sz_decimals)
        if size <= 0:
            return
        szi, entry = self.positions.get(req.coin, [0.0, 0.0])
        signed = size if req.is_buy else -size
        closed_pnl = 0.0
        if szi and (szi > 0) != (signed > 0):
            closing = min(size, abs(szi))
            closed_pnl = closing * (price - entry) * (1.0 if szi > 0 else -1.0)
        new_szi = round(szi + signed, 10)
        if abs(new_szi) < 1e-12:
            new_szi, new_entry = 0.0, 0.0
        elif szi == 0 or (szi > 0) == (signed > 0):
            new_entry = (abs(szi) * entry + size * price) / (abs(szi) + size)
        elif (new_szi > 0) != (szi > 0):
            new_entry = price  # flipped through zero
        else:
            new_entry = entry
        direction = ("Open " if szi == 0 or (szi > 0) == (signed > 0) else "Close ") + (
            ("Long" if signed > 0 else "Short") if szi == 0 or (szi > 0) == (signed > 0) else ("Long" if szi > 0 else "Short")
        )
        fee = price * size * (self.taker_fee if taker else self.maker_fee)
        self.balance += closed_pnl - fee
        self.positions[req.coin] = [new_szi, new_entry]
        order.remaining = round(order.remaining - size, 10)
        now = self.clock.now_ms()
        order.status_ms = now
        if order.remaining <= 1e-12:
            order.remaining = 0.0
            order.status = "filled"
        anonymous = self.faults.anonymous_trigger_fills and req.kind == "trigger"
        fill = Fill(
            tid=self._next_tid,
            oid=order.oid + 500000 if anonymous else order.oid,
            coin=req.coin,
            is_buy=req.is_buy,
            price=price,
            size=size,
            time_ms=now,
            fee=fee,
            fee_token="USDC",
            closed_pnl=closed_pnl,
            cloid=None if anonymous else (req.cloid or None),
            crossed=taker,
            dir=direction,
            start_position=szi,
            hash=f"0x{self._next_tid:064x}",
            liquidation=liquidation,
        )
        self._next_tid += 1
        self.fills.append(fill)
        self._emit("on_fill", fill)
        if order.status == "filled":
            self._emit_update(order)

    def _match(self, coin: str) -> None:
        """Fill or trigger resting orders against the current quote."""
        quote = self.quotes.get(coin)
        mark = self._mark(coin)
        if quote is None:
            return
        fills_before, events_before = len(self.fills), self._events.qsize()
        for order in sorted((o for o in self.orders.values() if o.status == "open" and o.req.coin == coin), key=lambda o: o.oid):
            req = order.req
            if req.kind == "trigger":
                if mark is None or req.trigger_px is None:
                    continue
                closing_long = not req.is_buy
                if req.tpsl == "sl":
                    fired = mark <= req.trigger_px if closing_long else mark >= req.trigger_px
                else:
                    fired = mark >= req.trigger_px if closing_long else mark <= req.trigger_px
                if not fired:
                    continue
                price = quote.bid if closing_long else quote.ask
                taker = True
            else:
                touch = quote.ask if req.is_buy else quote.bid
                if touch is None or not (touch <= req.price if req.is_buy else touch >= req.price):
                    continue
                price = req.price  # resting order fills at its own price
                taker = False
            if price is None:
                continue
            size = order.remaining
            if req.reduce_only:
                closable = self._closable(coin, req.is_buy)
                if closable <= 1e-12:
                    self._cancel(order, "reduceOnlyCanceled")
                    continue
                size = min(size, closable)  # IV-04: a reduce-only order is capped at the position
            if req.kind == "trigger":
                order.status = "triggered"
                order.status_ms = self.clock.now_ms()
                self._emit_update(order)
                order.status = "open"
            self._fill(order, price, size, taker=taker)
            if order.status == "open" and req.reduce_only and self._closable(coin, req.is_buy) <= 1e-12:
                self._cancel(order, "reduceOnlyCanceled")
        if len(self.fills) != fills_before or self._events.qsize() != events_before:
            self._changed()

    def _cancel(self, order: _Order, status: str = "canceled") -> None:
        order.status = status
        order.status_ms = self.clock.now_ms()
        self._emit_update(order)

    async def cancel(self, coin: str, cloids: Sequence[str], nonce: int) -> list[Optional[ExchangeError]]:
        self._action("cancel", nonce, list(cloids))
        results: list[Optional[ExchangeError]] = []
        for cloid in cloids:
            if self.faults.cancel_text:
                results.append(to_error(self.faults.cancel_text))
                continue
            oid = self.by_cloid.get(cloid)
            order = self.orders.get(oid) if oid is not None else None
            if order is None or order.status != "open":
                results.append(to_error(ERR_NOT_FOUND))
                continue
            self._cancel(order)
            results.append(None)
        self._changed()
        return results

    async def modify(self, cloid: str, new: OrderRequest, nonce: int) -> PlaceResult:
        self._action("modify", nonce, {"cloid": cloid, "new": new.cloid})
        oid = self.by_cloid.get(cloid)
        order = self.orders.get(oid) if oid is not None else None
        if order is None or order.status != "open":
            return self._error("Cannot modify canceled or filled order")
        inst = self.instruments.get(new.coin)
        if inst is None or not is_valid_price(new.price, inst.sz_decimals) or not is_valid_size(new.size, inst.sz_decimals):
            return self._error(ERR_TICK)
        if new.kind == "trigger":
            mark = self._mark(new.coin)
            closing_long = not new.is_buy
            assert new.trigger_px is not None
            if mark is not None and new.tpsl == "sl" and (new.trigger_px >= mark if closing_long else new.trigger_px <= mark):
                return self._error(ERR_TPSL)
        # A modify replaces the order: new oid, same or new cloid.
        del self.by_cloid[cloid]
        del self.orders[order.oid]
        replaced = self._register(new, "open", self.clock.now_ms())
        self._emit_update(replaced)
        self._changed()
        return PlaceResult("resting", oid=replaced.oid)

    # ------------------------------------------------------------ streams
    async def start(self, handlers: StreamHandlers) -> None:
        self._handlers = handlers
        self._health.connected = True
        if handlers.on_connection:
            await handlers.on_connection(True)

    async def stop(self) -> None:
        await self.drain()

    def stream_health(self) -> StreamHealth:
        return self._health

    def rest_weight_last_minute(self) -> int:
        return 0

    # ------------------------------------------------------------ out-of-band events (tests)
    async def external_cancel(self, cloid: str, status: str = "canceled") -> None:
        """Someone cancels one of our orders outside the bot."""
        order = self.orders[self.by_cloid[cloid]]
        self._cancel(order, status)
        self._changed()
        await self.drain()

    async def external_trade(self, coin: str, is_buy: bool, size: float, cloid: str = "") -> None:
        """A foreign order (no bot cloid) trades at the touch."""
        quote = self.quotes[coin]
        price = quote.ask if is_buy else quote.bid
        assert price is not None
        req = OrderRequest(coin, is_buy, size, price, reduce_only=False, cloid=cloid, kind="limit", tif="Ioc")
        order = _Order(self._next_oid, req, "open", size, size, self.clock.now_ms(), self.clock.now_ms())
        self._next_oid += 1
        self.orders[order.oid] = order
        self._fill(order, price, size, taker=True)
        self._changed()
        await self.drain()

    async def liquidate(self, coin: str) -> None:
        """The exchange liquidates the position at mark."""
        szi = self._szi(coin)
        if not szi:
            return
        mark = self._mark(coin) or self.positions[coin][1]
        for order in [o for o in self.orders.values() if o.status == "open" and o.req.coin == coin]:
            self._cancel(order, "liquidatedCanceled")
        req = OrderRequest(coin, szi < 0, abs(szi), mark, reduce_only=True, cloid="", kind="limit", tif="Ioc")
        order = _Order(self._next_oid, req, "open", abs(szi), abs(szi), self.clock.now_ms(), self.clock.now_ms())
        self._next_oid += 1
        self.orders[order.oid] = order
        self._fill(order, mark, abs(szi), taker=True, liquidation=True)
        if self._handlers.on_liquidation:
            self._emit("on_liquidation", {"coin": coin, "szi": szi, "mark": mark})
        self._changed()
        await self.drain()
