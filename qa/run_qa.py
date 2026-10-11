"""Testnet QA: 20 planned trade cases across BTC, ETH and SOL.

Two layers are exercised, because the application has no plain market or
resting-limit entry of its own:

* exchange layer (cases 1-12, 19, 20): orders placed straight through the
  bot's own exchange adapter (``hlbot.exchange.hyperliquid``), the same
  code path the bot signs and sends with;
* application layer (cases 13-18): the bot itself, started with a
  temporary settings file and its own data folder, fed injected signals.

Safety: refuses to do anything unless the configured environment and both
endpoints are the testnet, the account is flat on the test symbols, and
no other bot instance is running on the account. Every order carries a
unique client order id. Each case cleans up after itself, and a final
sweep cancels and closes whatever is left and reports it.

    .venv/bin/python qa/run_qa.py --preflight     # checks only, no orders
    .venv/bin/python qa/run_qa.py                 # the 20 cases

A failed case stays failed: nothing here retries a case to make it pass.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hlbot.config import load_settings, parse_env_file  # noqa: E402
from hlbot.core.app import build_adapter  # noqa: E402
from hlbot.domain.rounding import round_price, size_step, tick_size  # noqa: E402
from hlbot.exchange.base import ExchangeError, OrderRequest  # noqa: E402
from hlbot.timeutil import Clock  # noqa: E402

log = logging.getLogger("qa")
TESTNET_MARK = "hyperliquid-testnet.xyz"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def plain(value: Any) -> Any:
    """Dataclasses, exceptions and floats into JSON-friendly values."""
    if isinstance(value, ExchangeError):
        return {"error_class": str(value.kind), "text": str(value)}
    if isinstance(value, BaseException):
        return {"exception": type(value).__name__, "text": str(value)[:300]}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def same(a: Optional[float], b: Optional[float], tol: float = 1e-9) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


# ====================================================================== one test case
class Case:
    def __init__(self, case_id: str, scenario: str, symbol: str, layer: str, title: str, expected: str):
        self.id, self.scenario, self.symbol, self.layer, self.title, self.expected = case_id, scenario, symbol, layer, title, expected
        self.status = "not_run"
        self.started_at = self.finished_at = None
        self.steps: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.orders: list[dict[str, Any]] = []
        self.fees = 0.0
        self.realized_pnl = 0.0
        self.final_state: dict[str, Any] = {}
        self.blocked_reason: Optional[str] = None
        self.error: Optional[str] = None

    def step(self, action: str, latency_ms: Optional[float] = None, **data: Any) -> None:
        entry = {"at": now_iso(), "action": action, **({"latency_ms": round(latency_ms, 1)} if latency_ms is not None else {}), **plain(data)}
        self.steps.append(entry)
        log.info("%s | %s | %s", self.id, action, json.dumps({k: v for k, v in entry.items() if k not in ("at", "action")}, default=str)[:600])

    def check(self, name: str, passed: bool, expected: Any = None, actual: Any = None) -> bool:
        self.assertions.append({"name": name, "passed": bool(passed), "expected": plain(expected), "actual": plain(actual)})
        log.info("%s | %s | %s | expected=%s actual=%s", self.id, "PASS" if passed else "FAIL", name, plain(expected), plain(actual))
        return bool(passed)

    def block(self, reason: str) -> None:
        self.blocked_reason = reason
        log.warning("%s | BLOCKED | %s", self.id, reason)

    def finish(self) -> None:
        self.finished_at = now_iso()
        if self.error:
            self.status = "failed"
        elif self.blocked_reason:
            self.status = "blocked"
        elif not self.assertions:
            self.status = "skipped"
        else:
            self.status = "passed" if all(a["passed"] for a in self.assertions) else "failed"
        log.info("%s | RESULT %s (%d/%d assertions passed)", self.id, self.status.upper(), sum(a["passed"] for a in self.assertions), len(self.assertions))

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id, "scenario": self.scenario, "symbol": self.symbol, "layer": self.layer, "title": self.title, "expected": self.expected,
            "status": self.status, "started_at": self.started_at, "finished_at": self.finished_at,
            "assertions_passed": sum(a["passed"] for a in self.assertions), "assertions_total": len(self.assertions),
            "fees_usdc": round(self.fees, 6), "realized_pnl_usdc": round(self.realized_pnl, 6),
            "blocked_reason": self.blocked_reason, "error": self.error, "notes": self.notes,
            "orders": self.orders, "assertions": self.assertions, "final_state": self.final_state, "steps": self.steps,
        }


# ====================================================================== the exchange, through the bot's adapter
class Venue:
    def __init__(self, adapter: Any, run_id: str, cfg: dict[str, Any]):
        self.a, self.run_id, self.cfg = adapter, run_id, cfg
        self.instruments: dict[str, Any] = {}
        self._nonce = 0
        self._serial = 0
        self.latencies: list[dict[str, Any]] = []
        self.created: list[tuple[str, str]] = []  # (coin, cloid) of everything this run placed directly
        self.read_failures: list[dict[str, Any]] = []

    def nonce(self) -> int:
        self._nonce = max(int(time.time() * 1000), self._nonce + 1)
        return self._nonce

    def cloid(self, case: Case, label: str) -> str:
        self._serial += 1
        return "0x" + hashlib.sha256(f"qa|{self.run_id}|{case.id}|{label}|{self._serial}".encode()).hexdigest()[:32]

    async def timed(self, case: Optional[Case], kind: str, coro: Any) -> tuple[Any, float]:
        started = time.perf_counter()
        try:
            result = await coro
        except ExchangeError as exc:
            result = exc
        elapsed = (time.perf_counter() - started) * 1000
        self.latencies.append({"case": case.id if case else None, "kind": kind, "ms": round(elapsed, 1)})
        return result, elapsed

    def size_for(self, coin: str, price: float, notional: float) -> float:
        step = size_step(self.instruments[coin].sz_decimals)
        return round(math.ceil(notional / price / step - 1e-9) * step, self.instruments[coin].sz_decimals)

    def px(self, coin: str, price: float, mode: str = "nearest") -> float:
        return round_price(price, self.instruments[coin].sz_decimals, mode)

    async def book(self, case: Optional[Case], coin: str) -> Any:
        quote, ms = await self.timed(case, "book", self.a.get_book(coin))
        if isinstance(quote, ExchangeError) or quote.bid is None or quote.ask is None:
            raise RuntimeError(f"no two-sided book for {coin}: {plain(quote)}")
        if case:
            case.step("book", ms, bid=quote.bid, ask=quote.ask, bid_size=quote.bid_size, ask_size=quote.ask_size)
        return quote

    async def read(self, kind: str, call: Any) -> Any:
        """A read-only query, retried on a transport error (reads are idempotent). Every failed attempt is counted."""
        for attempt in range(4):
            started = time.perf_counter()
            try:
                result = await call()
                self.latencies.append({"case": None, "kind": f"read:{kind}", "ms": round((time.perf_counter() - started) * 1000, 1)})
                return result
            except ExchangeError as exc:
                self.read_failures.append({"at": now_iso(), "kind": kind, "attempt": attempt + 1, "error": str(exc)[:160], "after_ms": round((time.perf_counter() - started) * 1000, 1)})
                log.warning("read %s failed (attempt %d): %s", kind, attempt + 1, exc)
                if attempt == 3:
                    raise
                await asyncio.sleep(0.5)

    async def szi(self, coin: str) -> float:
        return (await self.read("account", self.a.get_account)).szi(coin)

    async def position(self, coin: str) -> Optional[Any]:
        return (await self.read("account", self.a.get_account)).position(coin)

    async def open_orders(self, coin: str) -> list[Any]:
        return [o for o in await self.read("open_orders", self.a.get_open_orders) if o.coin == coin]

    async def place(self, case: Case, label: str, coin: str, is_buy: bool, size: float, price: float, *, tif: str = "Gtc", reduce_only: bool = False,
                    cloid: Optional[str] = None) -> tuple[str, Any]:
        cloid = cloid or self.cloid(case, label)
        request = OrderRequest(coin=coin, is_buy=is_buy, size=size, price=price, reduce_only=reduce_only, cloid=cloid, tif=tif)
        self.created.append((coin, cloid))
        outcome, ms = await self.timed(case, "place", self.a.place([request], self.nonce()))
        result = outcome if isinstance(outcome, ExchangeError) else outcome[0]
        case.step(f"place {label}", ms, request=request, response=result)
        case.orders.append({"label": label, "cloid": cloid, "coin": coin, "side": "BUY" if is_buy else "SELL", "type": f"limit/{tif}", "size": size, "price": price,
                            "reduce_only": reduce_only, "oid": getattr(result, "oid", None), "immediate_status": getattr(result, "status", "error"),
                            "filled_size": getattr(result, "total_size", None), "avg_price": getattr(result, "avg_price", None),
                            "error": plain(result) if isinstance(result, ExchangeError) else plain(getattr(result, "error", None))})
        return cloid, result

    async def status(self, case: Case, cloid: str, want: Optional[str] = None, tries: int = 8) -> Any:
        """Order status by client order id; waits briefly for ``want``."""
        found = None
        for _ in range(tries):
            found, ms = await self.timed(case, "order_status", self.a.get_order(cloid))
            if not isinstance(found, ExchangeError) and found.found and (want is None or found.status == want):
                break
            await asyncio.sleep(0.4)
        case.step("order status", ms, cloid=cloid, status=found)
        for order in case.orders:
            if order["cloid"] == cloid and not isinstance(found, ExchangeError):
                order["final_status"] = found.status
        return found

    async def cancel(self, case: Case, coin: str, cloid: str) -> Any:
        outcome, ms = await self.timed(case, "cancel", self.a.cancel(coin, [cloid], self.nonce()))
        result = outcome if isinstance(outcome, ExchangeError) else outcome[0]
        case.step("cancel", ms, cloid=cloid, response=result if result is not None else "success")
        return result

    async def fills(self, case: Case, since_ms: int, cloids: list[str], expect: int = 1) -> list[Any]:
        """Fills of this case's orders; the fills endpoint can lag a moment behind the order response."""
        wanted = {c.lower() for c in cloids}
        mine: list[Any] = []
        for _ in range(8):
            all_fills, _ms = await self.timed(case, "fills", self.a.get_fills(since_ms))
            mine = [] if isinstance(all_fills, ExchangeError) else [f for f in all_fills if (f.cloid or "").lower() in wanted]
            if len({(f.cloid or "").lower() for f in mine}) >= expect:
                break
            await asyncio.sleep(0.5)
        case.fees += sum(f.fee for f in mine)
        case.realized_pnl += sum(f.closed_pnl for f in mine)
        case.step("fills", fills=[{"cloid": f.cloid, "side": "BUY" if f.is_buy else "SELL", "price": f.price, "size": f.size, "fee": f.fee,
                                   "closed_pnl": f.closed_pnl, "dir": f.dir, "crossed": f.crossed} for f in mine])
        return mine

    async def flatten(self, case: Optional[Case], coin: str) -> float:
        """Close whatever position is open on ``coin`` reduce-only. Returns the size left."""
        for attempt in range(3):
            szi = await self.szi(coin)
            if abs(szi) < 1e-12:
                return 0.0
            quote = await self.a.get_book(coin)
            slip = self.cfg["market_slippage_pct"] / 100 * (attempt + 1)
            price = self.px(coin, quote.bid * (1 - slip), "down") if szi > 0 else self.px(coin, quote.ask * (1 + slip), "up")
            holder = case or Case("CLEANUP", "cleanup", coin, "exchange", "cleanup", "")
            await self.place(holder, f"close {coin}", coin, szi < 0, abs(szi), price, tif="Ioc", reduce_only=True)
            await asyncio.sleep(0.6)
        return await self.szi(coin)

    async def sweep(self, case: Optional[Case], coin: str) -> dict[str, Any]:
        """Cancel every resting order on ``coin`` that this run can name, close the position, report what is left."""
        for order in await self.open_orders(coin):
            if order.cloid:
                await self.timed(case, "cancel", self.a.cancel(coin, [order.cloid], self.nonce()))
        left = await self.flatten(case, coin)
        await asyncio.sleep(0.4)
        remaining = await self.open_orders(coin)
        return {"position": left, "open_orders": [plain(o) for o in remaining]}


# ====================================================================== exchange-layer cases
async def case_market(v: Venue, case: Case, is_buy: bool) -> None:
    """A market order on Hyperliquid is an IOC limit with a slippage cap; this is what the bot's close uses."""
    coin = case.symbol
    started_ms = int(time.time() * 1000) - 2000
    before = await v.szi(coin)
    quote = await v.book(case, coin)
    touch = quote.ask if is_buy else quote.bid
    size = v.size_for(coin, touch, v.cfg["direct_notional_usd"])
    slip = v.cfg["market_slippage_pct"] / 100
    limit = v.px(coin, touch * (1 + slip), "up") if is_buy else v.px(coin, touch * (1 - slip), "down")
    cloid, result = await v.place(case, "market " + ("buy" if is_buy else "sell"), coin, is_buy, size, limit, tif="Ioc")
    accepted = case.check("order accepted by the exchange", not isinstance(result, ExchangeError) and result.error is None, "no error", result)
    if accepted:
        case.check("immediate status is filled", result.status == "filled", "filled", result.status)
        case.check("filled quantity equals the requested quantity", same(result.total_size, size), size, result.total_size)
        inside = result.avg_price is not None and (result.avg_price <= limit if is_buy else result.avg_price >= limit)
        case.check("execution price is inside the slippage cap", inside, f"{'<=' if is_buy else '>='} {limit}", result.avg_price)
        if result.avg_price:
            slippage_bps = (result.avg_price - touch) / touch * 1e4 * (1 if is_buy else -1)
            case.notes.append(f"expected price (touch) {touch}, actual {result.avg_price}, slippage {slippage_bps:.2f} bps")
        status = await v.status(case, cloid, "filled")
        case.check("order status by client id is filled", getattr(status, "status", None) == "filled", "filled", getattr(status, "status", None))
        pos = await v.position(coin)
        expected_szi = before + (size if is_buy else -size)
        case.check("position size matches the fill", same(pos.szi if pos else 0.0, expected_szi), expected_szi, pos.szi if pos else 0.0)
        case.step("position", position=pos)
        if pos and result.avg_price:
            case.check("position entry price equals the fill price", pos.entry_px is not None and abs(pos.entry_px - result.avg_price) / result.avg_price < 1e-4,
                       result.avg_price, pos.entry_px)
            case.notes.append(f"margin used {pos.margin_used}, leverage {pos.leverage} {pos.leverage_type}, unrealized PnL {pos.unrealized_pnl}")
    left = await v.flatten(case, coin)
    case.check("test position closed (flat after the case)", abs(left - before) < 1e-12, before, left)
    fills = await v.fills(case, started_ms, [o["cloid"] for o in case.orders], expect=2 if accepted else 0)
    if accepted:
        case.check("fills carry our client order ids and a fee was charged", len(fills) >= 2 and case.fees > 0, ">= 2 fills, fee > 0", f"{len(fills)} fills, fee {case.fees}")
    rest = await v.open_orders(coin)
    case.check("no open orders left on the symbol", not rest, [], rest)
    case.final_state = {"position": await v.szi(coin), "open_orders": len(rest)}


async def case_limit(v: Venue, case: Case, is_buy: bool, mode: str) -> None:
    """mode: 'resting' (GTC away from the market, then cancelled), 'post_only' (ALO, then cancelled),
    'marketable' (GTC at the touch: fills as a taker, then the position is closed)."""
    coin = case.symbol
    started_ms = int(time.time() * 1000) - 2000
    before = await v.szi(coin)
    quote = await v.book(case, coin)
    away = v.cfg["resting_offset_pct"] / 100
    if mode == "marketable":
        price = v.px(coin, quote.ask, "up") if is_buy else v.px(coin, quote.bid, "down")
    elif mode == "post_only":
        price = v.px(coin, quote.bid * (1 - away / 6), "down") if is_buy else v.px(coin, quote.ask * (1 + away / 6), "up")
    else:
        price = v.px(coin, quote.bid * (1 - away), "down") if is_buy else v.px(coin, quote.ask * (1 + away), "up")
    size = v.size_for(coin, price, v.cfg["direct_notional_usd"])
    tif = "Alo" if mode == "post_only" else "Gtc"
    cloid, result = await v.place(case, f"limit {'buy' if is_buy else 'sell'} ({mode})", coin, is_buy, size, price, tif=tif)
    accepted = case.check("order accepted by the exchange", not isinstance(result, ExchangeError) and result.error is None, "no error", result)
    if accepted and mode == "marketable":
        case.check("a limit at the touch fills immediately", result.status == "filled", "filled", result.status)
        case.check("filled quantity equals the requested quantity", same(result.total_size, size), size, result.total_size)
        better = result.avg_price is not None and (result.avg_price <= price if is_buy else result.avg_price >= price)
        case.check("execution price is at the limit or better", better, f"{'<=' if is_buy else '>='} {price}", result.avg_price)
        pos = await v.position(coin)
        expected_szi = before + (size if is_buy else -size)
        case.check("position size matches the fill", same(pos.szi if pos else 0.0, expected_szi), expected_szi, pos.szi if pos else 0.0)
        case.step("position", position=pos)
        status = await v.status(case, cloid, "filled")
        case.check("order status by client id is filled", getattr(status, "status", None) == "filled", "filled", getattr(status, "status", None))
    elif accepted:
        case.check("immediate status is resting (open)", result.status == "resting" and result.oid is not None, "resting with an order id", f"{result.status} oid={result.oid}")
        listed = [o for o in await v.open_orders(coin) if (o.cloid or "").lower() == cloid.lower()]
        case.check("order is listed in open orders", len(listed) == 1, 1, len(listed))
        if listed:
            o = listed[0]
            ok = same(o.price, price) and same(o.size, size) and o.is_buy == is_buy and not o.reduce_only and not o.is_trigger
            case.check("open order has the requested price, size and side", ok, {"price": price, "size": size, "is_buy": is_buy}, o)
        status = await v.status(case, cloid, "open")
        case.check("order status by client id is open", getattr(status, "status", None) == "open", "open", getattr(status, "status", None))
        case.check("a resting order creates no position", same(await v.szi(coin), before), before, await v.szi(coin))
        cancelled = await v.cancel(case, coin, cloid)
        case.check("cancel accepted", cancelled is None, "success", cancelled)
        status = await v.status(case, cloid, "canceled")
        case.check("order status after cancel is canceled", getattr(status, "status", None) == "canceled", "canceled", getattr(status, "status", None))
    left = await v.flatten(case, coin)
    case.check("flat after the case", abs(left - before) < 1e-12, before, left)
    if accepted and mode == "marketable":
        fills = await v.fills(case, started_ms, [o["cloid"] for o in case.orders], expect=2)
        case.check("fills carry our client order ids and a fee was charged", len(fills) >= 2 and case.fees > 0, ">= 2 fills, fee > 0", f"{len(fills)} fills, fee {case.fees}")
    rest = await v.open_orders(coin)
    case.check("no open orders left on the symbol", not rest, [], rest)
    case.final_state = {"position": await v.szi(coin), "open_orders": len(rest)}


async def case_cancel_replace(v: Venue, case: Case) -> None:
    coin = case.symbol
    before = await v.szi(coin)
    quote = await v.book(case, coin)
    away = v.cfg["resting_offset_pct"] / 100
    first_px, second_px, third_px = (v.px(coin, quote.bid * (1 - away * k), "down") for k in (1.0, 0.8, 0.6))
    size = v.size_for(coin, third_px, v.cfg["direct_notional_usd"])
    cloid, result = await v.place(case, "resting buy", coin, True, size, first_px)
    if not case.check("original order rests", not isinstance(result, ExchangeError) and result.status == "resting", "resting", result):
        await v.sweep(case, coin)
        return
    # ---- replace in place (modify): same client order id, new price
    replacement = OrderRequest(coin=coin, is_buy=True, size=size, price=second_px, reduce_only=False, cloid=cloid, tif="Gtc")
    outcome, ms = await v.timed(case, "modify", v.a.modify(cloid, replacement, v.nonce()))
    case.step("modify (replace in place)", ms, request=replacement, response=outcome)
    case.check("modify accepted", not isinstance(outcome, ExchangeError) and outcome.error is None and outcome.status == "resting", "resting", outcome)
    listed = [o for o in await v.open_orders(coin) if (o.cloid or "").lower() == cloid.lower()]
    case.check("exactly one open order carries the client id after the modify", len(listed) == 1, 1, len(listed))
    case.check("the open order has the new price and the same size", bool(listed) and same(listed[0].price, second_px) and same(listed[0].size, size),
               {"price": second_px, "size": size}, listed[0] if listed else None)
    if listed and result.oid:
        case.notes.append(f"exchange order id before modify {result.oid}, after {listed[0].oid} ({'kept' if listed[0].oid == result.oid else 'changed'}); client id kept")
    # ---- cancel, then a brand-new order (cancel + replace)
    cancelled = await v.cancel(case, coin, cloid)
    case.check("cancel accepted", cancelled is None, "success", cancelled)
    status = await v.status(case, cloid, "canceled")
    case.check("old order is canceled", getattr(status, "status", None) == "canceled", "canceled", getattr(status, "status", None))
    again = await v.cancel(case, coin, cloid)
    case.check("cancelling it a second time is refused, not silently accepted", again is not None, "an error", again)
    new_cloid, new = await v.place(case, "replacement buy", coin, True, size, third_px)
    case.check("replacement order rests under a new client id", not isinstance(new, ExchangeError) and new.status == "resting" and new_cloid != cloid, "resting", new)
    listed = await v.open_orders(coin)
    case.check("only the replacement is open", [(o.cloid or "").lower() for o in listed] == [new_cloid.lower()], [new_cloid], [o.cloid for o in listed])
    cancelled = await v.cancel(case, coin, new_cloid)
    case.check("replacement cancelled in cleanup", cancelled is None, "success", cancelled)
    rest = await v.open_orders(coin)
    case.check("no open orders left and no position was created", not rest and same(await v.szi(coin), before), {"orders": 0, "position": before},
               {"orders": len(rest), "position": await v.szi(coin)})
    case.final_state = {"position": await v.szi(coin), "open_orders": len(rest)}


async def case_invalid(v: Venue, case: Case) -> None:
    """Negative subtests. Each one is built so that it cannot become a position even if the exchange accepted it:
    resting prices far from the market, or reduce-only with nothing to reduce."""
    coin = case.symbol
    before = await v.szi(coin)
    quote = await v.book(case, coin)
    inst = v.instruments[coin]
    far_buy = v.px(coin, quote.bid * 0.97, "down")
    ok_size = v.size_for(coin, far_buy, v.cfg["direct_notional_usd"])

    async def expect_rejected(label: str, **order: Any) -> None:
        try:
            cloid, result = await v.place(case, label, **order)
        except Exception as exc:  # noqa: BLE001  a local refusal is also a rejection
            case.step(f"{label}: refused before sending", error=exc)
            case.check(f"{label}: rejected", True, "rejected", f"refused locally: {type(exc).__name__}: {str(exc)[:120]}")
            return
        rejected = isinstance(result, ExchangeError) or result.error is not None or result.status == "error"
        reason = plain(result if isinstance(result, ExchangeError) else result.error)
        case.check(f"{label}: rejected", rejected, "rejected", reason if rejected else result)
        if not rejected:  # it was accepted: take it away again and say so
            case.notes.append(f"{label}: ACCEPTED by the exchange as {result.status}; cancelled in cleanup")
            await v.cancel(case, order["coin"], cloid)

    # a. below the 10 USDC minimum order value
    await expect_rejected("a. order value below the 10 USDC minimum", coin=coin, is_buy=True, size=v.size_for(coin, far_buy, 4.0), price=far_buy)
    # b. reduce-only with no position to reduce
    await expect_rejected("b. reduce-only sell with no position", coin=coin, is_buy=False, size=ok_size, price=v.px(coin, quote.bid * 0.99, "down"), tif="Ioc", reduce_only=True)
    # c. price with more precision than the tick allows
    await expect_rejected("c. price off the tick grid", coin=coin, is_buy=True, size=ok_size, price=round(far_buy + tick_size(far_buy, inst.sz_decimals) * 0.37, 8))
    # d. size with more decimals than the symbol allows
    await expect_rejected("d. size with too many decimals", coin=coin, is_buy=True, size=round(ok_size + size_step(inst.sz_decimals) / 4, 8), price=far_buy)
    # e. zero size
    await expect_rejected("e. zero size", coin=coin, is_buy=True, size=0.0, price=far_buy)
    # f. a sell limit ten times above the market (resting far away if it were accepted)
    await expect_rejected("f. price ten times the market", coin=coin, is_buy=False, size=ok_size, price=v.px(coin, quote.ask * 10, "up"))
    # g. a symbol that does not exist
    await expect_rejected("g. unknown symbol", coin="NOPE", is_buy=True, size=1.0, price=1.0)
    # h. post-only order that would cross the book
    await expect_rejected("h. post-only buy priced through the ask", coin=coin, is_buy=True, size=ok_size, price=v.px(coin, quote.ask * 1.001, "up"), tif="Alo")
    # i. the same client order id used twice
    cloid, first = await v.place(case, "i. resting order (first use of the client id)", coin, True, ok_size, far_buy)
    if not isinstance(first, ExchangeError) and first.status == "resting":
        await expect_rejected("i. second order with the same client id", coin=coin, is_buy=True, size=ok_size, price=v.px(coin, quote.bid * 0.96, "down"), cloid=cloid)
        listed = [o for o in await v.open_orders(coin) if (o.cloid or "").lower() == cloid.lower()]
        case.check("i. still exactly one order under that client id", len(listed) == 1, 1, len(listed))
        await v.cancel(case, coin, cloid)
        for orphan in [o for o in await v.open_orders(coin) if (o.cloid or "").lower() == cloid.lower()]:
            # the client id no longer reaches this order (seen on testnet): cancel it by exchange order id
            outcome, ms = await v.timed(case, "cancel_by_oid", v.a._exchange({"type": "cancel", "cancels": [{"a": inst.asset_id, "o": orphan.oid}]}, v.nonce()))
            case.step("cancel by exchange order id (the client id no longer reaches it)", ms, oid=orphan.oid, response=outcome if isinstance(outcome, ExchangeError) else "success")
            case.notes.append(f"i. order {orphan.oid} could not be cancelled by its client id after the duplicate was cancelled; removed by order id")
    else:
        case.check("i. first order with the client id rests", False, "resting", first)
    left = await v.sweep(case, coin)
    case.check("no subtest created a position or left an order", abs(left["position"] - before) < 1e-12 and not left["open_orders"], {"position": before, "open_orders": 0},
               {"position": left["position"], "open_orders": len(left["open_orders"])})
    case.final_state = {"position": left["position"], "open_orders": len(left["open_orders"])}


# ====================================================================== the bot, started with a temporary configuration
class BotSession:
    """The real application, one symbol at a time (its fixed size and point settings are global, not per symbol)."""

    def __init__(self, v: Venue, run_dir: Path, base_env: dict[str, str], cfg: dict[str, Any]):
        self.v, self.run_dir, self.base_env, self.cfg = v, run_dir, base_env, cfg
        self.proc: Optional[subprocess.Popen] = None
        self.coin = ""
        self.size = 0.0
        self.http: Optional[aiohttp.ClientSession] = None
        self.base = f"http://127.0.0.1:{cfg['api_port']}"
        self.data_dir = run_dir / "bot" / "data"
        self.token = ""
        self.serial = 0  # signals sent in this run
        self.tp_levels = 5  # take-profit orders the bot is configured to place (EXEC_TP_CLOSE_PERCENTS has one share per level)

    async def start(self, coin: str) -> dict[str, Any]:
        quote = await self.v.a.get_book(coin)
        price = (quote.bid + quote.ask) / 2
        inst = self.v.instruments[coin]
        tp_size = self.v.size_for(coin, price * 0.985, self.cfg["bot_tp_notional_usd"])  # every take-profit leg above the 10 USDC minimum
        self.coin, self.size = coin, round(tp_size * 5, inst.sz_decimals)
        points = lambda pct: f"{max(price * pct / 100, 10 ** -6):.6f}"  # noqa: E731  the bot's point settings are absolute prices
        env = {
            **self.base_env,
            "HL_ENV": "testnet", "HL_SYMBOL_MAP": json.dumps({coin: coin}), "HL_MARGIN_MODE": "cross",
            "TELEGRAM_ENABLED": "false", "TELEGRAM_NOTIFY_ENABLED": "false",
            "RISK_SIZING_MODE": "fixed", "RISK_FIXED_SIZE": f"{self.size:.{inst.sz_decimals}f}", "RISK_LEVERAGE": str(self.cfg["bot_leverage"]),
            "RISK_MIN_RR": "0.01", "RISK_MAX_SL_DISTANCE": points(5.0), "RISK_SL_SLIPPAGE_ALLOWANCE_POINTS": points(0.01),
            "RISK_MAX_OPEN_TRADES": "3", "RISK_MAX_DAILY_LOSS_PCT": "2.0",
            "EXEC_BUFFER_ENABLED": "true", "EXEC_BUFFER_VALUE": points(0.02), "EXEC_MAX_SLIPPAGE_POINTS": points(0.15), "EXEC_MIN_TP_GAP_POINTS": points(0.004),
            "EXEC_TP_CLOSE_PERCENTS": "20,20,20,20,20", "EXEC_SL_RESIZE_ON_TP": "true",
            "DATA_DIR": str(self.data_dir), "LOG_DIR": str(self.run_dir / "bot" / "logs"),
            "API_ENABLED": "true", "API_HOST": "127.0.0.1", "API_PORT": str(self.cfg["api_port"]),
        }
        env_path = self.run_dir / f"bot-{coin}.env"
        env_path.write_text("# Temporary QA settings (testnet). No secrets: the agent key stays in its own owner-only file.\n" + "".join(f"{k}={v}\n" for k, v in env.items()))
        check = load_settings(environ={}, env_file=str(env_path))
        if check.env != "testnet" or TESTNET_MARK not in check.rest_url:
            raise RuntimeError("the temporary bot configuration is not testnet; refusing to start it")
        out = open(self.run_dir / "bot" / f"bot-{coin}.out", "w")  # noqa: SIM115
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("HL_", "EXEC_", "RISK_", "TELEGRAM_", "API_", "DATA_", "LOG_", "SECRETS_"))}
        self.proc = subprocess.Popen([sys.executable, "-m", "hlbot", "--env-file", str(env_path), "run", "--quiet"], cwd=str(ROOT), env=clean_env, stdout=out, stderr=out)
        self.http = aiohttp.ClientSession()
        deadline = time.time() + self.cfg["bot_start_timeout_s"]
        health: dict[str, Any] = {}
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the bot exited during start (code {self.proc.returncode}); see bot/bot-{coin}.out")
            try:
                async with self.http.get(self.base + "/health", timeout=aiohttp.ClientTimeout(total=2)) as response:
                    health = await response.json()
                if health.get("health") == "HEALTHY" and health.get("env") == "TESTNET":
                    self.token = (self.data_dir / "control.token").read_text().strip()
                    status = await self.status()
                    if status["live"].get("exchange_account") and status["live"]["quotes"].get(coin, {}).get("bid"):
                        log.info("bot session up for %s: size %s, health %s", coin, self.size, health)
                        return {"env_file": str(env_path), "fixed_size": self.size, "settings": {k: v for k, v in env.items() if k not in ("HL_ACCOUNT_ADDRESS", "HL_AGENT_KEY_FILE")}}
            except (TimeoutError, aiohttp.ClientError, OSError, KeyError):
                pass
            await asyncio.sleep(1)
        raise RuntimeError(f"the bot did not become healthy in {self.cfg['bot_start_timeout_s']} s (last health: {health})")

    async def stop(self) -> None:
        if self.http:
            await self.http.close()
            self.http = None
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self.proc.wait, 30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        await asyncio.sleep(1)

    async def status(self) -> dict[str, Any]:
        assert self.http
        async with self.http.get(self.base + "/api/status", headers={"X-Control-Token": self.token}, timeout=aiohttp.ClientTimeout(total=8)) as response:
            return await response.json()

    async def command(self, case: Case, name: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.http
        started = time.perf_counter()
        async with self.http.post(f"{self.base}/api/control/{name}", json={**body, "by": "qa"}, headers={"X-Control-Token": self.token},
                                  timeout=aiohttp.ClientTimeout(total=60)) as response:
            result = await response.json()
        ms = (time.perf_counter() - started) * 1000
        self.v.latencies.append({"case": case.id, "kind": f"bot:{name}", "ms": round(ms, 1)})
        case.step(f"bot command {name}", ms, request={k: (v if k != "text" else v.replace("\n", " | ")) for k, v in body.items()}, response=result)
        return result

    async def trade(self, trade_id: int) -> Optional[dict[str, Any]]:
        return next((t for t in (await self.status())["recent_trades"] if t["id"] == trade_id), None)

    async def wait_trade(self, trade_id: int, done: Any, timeout: float) -> Optional[dict[str, Any]]:
        deadline = time.time() + timeout
        trade = None
        while time.time() < deadline:
            trade = await self.trade(trade_id)
            if trade and done(trade):
                return trade
            await asyncio.sleep(0.7)
        return trade

    def signal_text(self, side: str, quote: Any, ladder_pct: list[float], stop_pct: float) -> tuple[str, dict[str, Any]]:
        """A signal in the channel's format whose zone holds the current price, so the bot enters at once."""
        coin, v = self.coin, self.v
        touch = quote.ask if side == "BUY" else quote.bid
        self.serial += 1
        half = max(touch * 0.0002, (quote.ask - quote.bid)) + self.serial * tick_size(touch, v.instruments[coin].sz_decimals)
        direction = 1 if side == "BUY" else -1
        low, high = v.px(coin, touch - half, "down"), v.px(coin, touch + half, "up")
        stop = v.px(coin, touch * (1 - direction * stop_pct / 100))
        tps = [v.px(coin, touch * (1 + direction * pct / 100)) for pct in ladder_pct]
        # every level must lie beyond the zone and beyond the level before it, whatever the tick size
        edge, tick = (high if side == "BUY" else low), tick_size(touch, v.instruments[coin].sz_decimals)
        for i in range(len(tps)):
            floor = v.px(coin, (edge if i == 0 else tps[i - 1]) + direction * tick)
            if (tps[i] - floor) * direction < 0:
                tps[i] = floor
        text = (f"#{coin} {side} NOW\n{coin} {side.lower()} Zone {low:.10g} - {high:.10g}\n\nSL : {stop:.10g}\n\n" +
                "".join(f"TP{i} : {tp:.10g}\n" for i, tp in enumerate(tps, 1)) + "#QA_Signals")
        return text, {"side": side, "zone": [low, high], "stop": stop, "take_profits": tps, "touch": touch}

    async def open_trade(self, case: Case, side: str, ladder_pct: list[float], stop_pct: float) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        """Inject a signal and wait until the trade is protected. Returns (trade, plan) or None with the reason recorded."""
        quote = await self.v.book(case, self.coin)
        text, plan = self.signal_text(side, quote, ladder_pct, stop_pct)
        case.step("signal built", plan=plan)
        result = await self.command(case, "inject", {"text": text, "channel": "qa"})
        if not case.check("signal accepted by the bot (parsed and valid)", bool(result.get("ok")) and result.get("state") in ("WAITING_PRICE", "ENTERING", "TRADED"),
                          "WAITING_PRICE / ENTERING / TRADED", {"state": result.get("state"), "reason": result.get("reason")}):
            return None
        signal_id = result["signal_id"]
        deadline = time.time() + 45
        trade = None
        while time.time() < deadline:
            status = await self.status()
            trade = next((t for t in status["recent_trades"] if t["signal_id"] == signal_id), None)
            if trade and trade["state"] not in ("ENTERING", "OPEN_UNPROTECTED"):
                break
            row = next((s for s in status["recent_signals"] if s["id"] == signal_id), None)
            if row and row["state"] in ("REJECTED_RISK", "FAILED", "EXPIRED", "INVALID", "DUPLICATE"):
                case.check("signal led to a trade", False, "a trade", {"signal_state": row["state"], "reason": row.get("reason")})
                return None
            await asyncio.sleep(0.5)
        if not case.check("trade reached PROTECTED", bool(trade) and trade["state"] == "PROTECTED", "PROTECTED", trade["state"] if trade else "no trade within 45 s"):
            return None
        case.step("trade protected", trade={k: trade[k] for k in ("id", "coin", "side", "state", "planned_size", "filled_size", "avg_entry", "sl_price", "opened_at", "protected_at")})
        try:
            took = (datetime.fromisoformat(trade["protected_at"]) - datetime.fromisoformat(trade["opened_at"])).total_seconds()
            case.notes.append(f"time from fill to protected: {took:.2f} s")
            self.v.latencies.append({"case": case.id, "kind": "bot:time_to_protected", "ms": round(took * 1000, 1)})
        except (TypeError, ValueError):
            pass
        return trade, plan

    async def verify_protection(self, case: Case, trade: dict[str, Any], plan: dict[str, Any]) -> None:
        """Compare what the exchange itself holds with what the signal asked for (queried directly, not through the bot)."""
        coin, v, long = self.coin, self.v, plan["side"] == "BUY"
        case.check("filled size equals the configured size", same(trade["filled_size"], self.size), self.size, trade["filled_size"])
        entry_ok = trade["avg_entry"] is not None and plan["zone"][0] * 0.999 <= trade["avg_entry"] <= plan["zone"][1] * 1.001
        case.check("entry price is inside the signal zone (with its buffer)", entry_ok, plan["zone"], trade["avg_entry"])
        slip = (trade["avg_entry"] - plan["touch"]) / plan["touch"] * 1e4 * (1 if long else -1) if trade["avg_entry"] else None
        case.notes.append(f"expected entry (touch) {plan['touch']}, actual {trade['avg_entry']}, slippage {slip:.2f} bps" if slip is not None else "no entry price")
        pos = await v.position(coin)
        want = self.size if long else -self.size
        case.check("exchange position matches the trade (direction and size)", same(pos.szi if pos else 0.0, want), want, pos.szi if pos else 0.0)
        case.step("exchange position", position=pos)
        if pos:
            case.check("leverage on the exchange is the configured leverage", pos.leverage == self.cfg["bot_leverage"], self.cfg["bot_leverage"], pos.leverage)
        orders = await v.open_orders(coin)
        case.step("exchange open orders", orders=orders)
        stops = [o for o in orders if o.is_trigger]
        tps = sorted((o for o in orders if not o.is_trigger), key=lambda o: o.price, reverse=not long)
        stop_ok = (len(stops) == 1 and stops[0].reduce_only and stops[0].is_buy != long and same(stops[0].size, self.size)
                   and same(stops[0].trigger_px, plan["stop"]) and bool(stops[0].cloid))
        case.check("one reduce-only stop-loss for the full size at the signal's stop price", stop_ok, {"trigger": plan["stop"], "size": self.size}, stops)
        case.check(f"{self.tp_levels} take-profit orders rest on the exchange, one per configured share", len(tps) == self.tp_levels, self.tp_levels, len(tps))
        case.check("every take-profit is reduce-only, on the closing side, with a client id",
                   all(o.reduce_only and o.is_buy != long and o.cloid for o in tps), True, [(o.reduce_only, o.is_buy, bool(o.cloid)) for o in tps])
        wanted = plan["take_profits"][: self.tp_levels]
        case.check("take-profit prices match the signal's first levels", [o.price for o in tps] == wanted, wanted, [o.price for o in tps])
        case.check("take-profit sizes add up to the position", same(sum(o.size for o in tps), self.size, 1e-9), self.size, round(sum(o.size for o in tps), 10))

    async def verify_closed(self, case: Case, trade_id: int, timeout: float = 40) -> Optional[dict[str, Any]]:
        trade = await self.wait_trade(trade_id, lambda t: t["state"] == "CLOSED", timeout)
        case.check("trade is CLOSED in the bot", bool(trade) and trade["state"] == "CLOSED", "CLOSED", trade["state"] if trade else None)
        await asyncio.sleep(1.0)
        szi, orders = await self.v.szi(self.coin), await self.v.open_orders(self.coin)
        case.check("exchange position is flat", abs(szi) < 1e-12, 0.0, szi)
        case.check("no orders of the trade are left on the exchange", not orders, [], orders)
        if trade:
            case.fees, case.realized_pnl = float(trade.get("fees") or 0.0), float(trade.get("realized_pnl") or 0.0)
            case.check("the bot recorded fees and a net result for the trade", trade.get("net_pnl") is not None and case.fees > 0, "net P/L set, fees > 0",
                       {"net_pnl": trade.get("net_pnl"), "fees": trade.get("fees")})
            case.orders = [{"label": (o["role"] + (str(o["leg"]) if o["role"] == "TP" else "")), "side": o["side"], "type": f"{o['type']}/{o['tif'] or '-'}", "price": o["price"],
                            "trigger_price": o["trigger_price"], "size": o["size"], "filled_size": o["filled_size"], "avg_price": o["avg_fill_price"],
                            "final_status": o["state"], "exchange_status": o["exchange_status"]} for o in trade["orders"]]
            case.notes.append(f"outcome {trade.get('outcome')}, close reason {trade.get('close_reason')}, realised {trade.get('realized_pnl')}, fees {trade.get('fees')}, "
                              f"funding {trade.get('funding')}, net {trade.get('net_pnl')}")
        case.final_state = {"trade_state": trade["state"] if trade else None, "position": szi, "open_orders": len(orders)}
        return trade


class LiveBot(BotSession):
    """The operator's own running bot. The runner only injects signals, sends commands and reads; it never places,
    cancels or closes anything on the exchange itself, so the bot stays the single writer on the account."""

    async def attach(self, coin: str, size: float) -> dict[str, Any]:
        self.coin, self.size = coin, size
        self.http = aiohttp.ClientSession()
        async with self.http.get(self.base + "/health", timeout=aiohttp.ClientTimeout(total=5)) as response:
            health = await response.json()
        if health.get("env") != "TESTNET":
            raise RuntimeError(f"the running bot is not on testnet ({health.get('env')}); nothing was sent")
        self.token = (self.data_dir / "control.token").read_text().strip()
        status = await self.status()
        state = status["health"]
        if state["state"] != "HEALTHY" or status["open_trades"] or coin not in status["live"]["quotes"]:
            raise RuntimeError(f"the running bot is not ready: health {state['state']} {[c['key'] for c in state['conditions']]}, "
                               f"{len(status['open_trades'])} open trade(s), coins {list(status['live']['quotes'])}")
        return {"health": health, "day": status.get("day"), "fixed_size": size}

    async def stop(self) -> None:  # the operator's bot keeps running
        if self.http:
            await self.http.close()
            self.http = None

    def signal_text(self, side: str, quote: Any, ladder_pct: list[float], stop_pct: float) -> tuple[str, dict[str, Any]]:
        """A signal shaped like the operator's channel posts them for this symbol: a zone around the price, the stop a
        few points beyond the zone, five take-profits stepping away from it. Fits the operator's own risk settings."""
        coin, v, cfg = self.coin, self.v, self.cfg
        self.serial += 1
        touch = quote.ask if side == "BUY" else quote.bid
        direction = 1 if side == "BUY" else -1
        # A different zone for every signal of the run (the bot refuses a repeated one), never wider than the
        # operator's maximum stop distance allows: 20 distinct (below, above) pairs.
        below, above = cfg["live_zone_half"] - 6 + self.serial % 5, cfg["live_zone_half"] - 6 + (self.serial // 5) % 4
        low, high = v.px(coin, touch - below, "down"), v.px(coin, touch + above, "up")
        near, far = (low, high) if side == "BUY" else (high, low)  # the stop lies beyond `near`, the targets beyond `far`
        stop = v.px(coin, near - direction * cfg["live_stop_beyond_zone"])
        tps = [v.px(coin, far + direction * offset) for offset in cfg["live_tp_beyond_zone"]]
        text = (f"#{coin} {side} NOW\n{coin} {side.lower()} Zone {low:.10g} - {high:.10g}\n\nSL : {stop:.10g}\n\n" +
                "".join(f"TP{i} : {tp:.10g}\n" for i, tp in enumerate(tps, 1)) + "\nor set your stop loss according to your margin\n#Vip_Signals")
        return text, {"side": side, "zone": [low, high], "stop": stop, "take_profits": tps, "touch": touch}


async def case_live(b: LiveBot, case: Case, side: str) -> None:
    """One real trade through the running bot: signal, entry, protection checked on the exchange, a short hold in which the
    market may reach a take-profit or the stop, then a close by command if it is still open."""
    opened = await b.open_trade(case, side, [], 0.0)
    if not opened:
        return
    trade, plan = opened
    await b.verify_protection(case, trade, plan)
    hold = b.cfg["live_hold_s"]
    case.step(f"holding up to {hold} s: the market may reach a take-profit or the stop")
    moved = await b.wait_trade(trade["id"], lambda t: t["state"] in ("CLOSED", "HALTED_MANUAL") or (t["remaining_size"] or 0) < (t["filled_size"] or 0) - 1e-12, hold)
    now = moved or await b.trade(trade["id"])
    took = [o for o in now["orders"] if o["role"] == "TP" and (o["filled_size"] or 0) > 0]
    stopped = [o for o in now["orders"] if o["role"] == "SL" and (o["filled_size"] or 0) > 0]
    if stopped:
        case.notes.append(f"the stop-loss triggered during the hold at {stopped[0]['avg_fill_price']} (signal stop {plan['stop']})")
        case.check("a triggered stop closed the whole position", now["state"] == "CLOSED" or same(now["remaining_size"], 0.0), "closed", {"state": now["state"], "remaining": now["remaining_size"]})
    elif took and now["state"] != "CLOSED":
        await asyncio.sleep(2.5)  # let the bot resize the stop after the fill
        now = await b.trade(trade["id"])
        took = [o for o in now["orders"] if o["role"] == "TP" and (o["filled_size"] or 0) > 0]
        closed_part = round(sum(o["filled_size"] for o in took), 10)
        case.notes.append(f"take-profit level(s) {[o['leg'] for o in took]} filled during the hold, closing {closed_part}")
        if now["state"] != "CLOSED":
            szi, orders = await b.v.szi(b.coin), await b.v.open_orders(b.coin)
            case.step("exchange after the partial close", position=szi, orders=orders, trade_state=now["state"], remaining=now["remaining_size"])
            case.check("after a take-profit fill the trade is REDUCING", now["state"] == "REDUCING", "REDUCING", now["state"])
            case.check("exchange position shrank to the remaining size", same(abs(szi), now["remaining_size"], 1e-9), now["remaining_size"], abs(szi))
            stops = [o for o in orders if o.is_trigger]
            case.check("stop-loss was resized to the remaining position", len(stops) == 1 and same(stops[0].size, now["remaining_size"], 1e-9), now["remaining_size"], [o.size for o in stops])
    current = await b.trade(trade["id"])
    if current and current["state"] != "CLOSED":
        result = await b.command(case, "close", {"trade_id": trade["id"]})
        case.check("close command accepted", bool(result.get("ok")), True, result)
    await b.verify_closed(case, trade["id"])


def plan_live20() -> list[Case]:
    return [Case(f"LV{i:02d}", "Long through the live bot" if i % 2 else "Short through the live bot", "BTC", "live bot",
                 f"Live bot: {'long' if i % 2 else 'short'} BTC from a channel-style signal",
                 "IOC entry in the zone, full-size reduce-only stop, five reduce-only take-profits, PROTECTED; ends by take-profit, stop or a close command, flat afterwards")
            for i in range(1, 21)]


FAR_LADDER = [0.5, 0.7, 0.9, 1.2, 1.6]   # take-profits out of reach for the minute a case lasts
NEAR_LADDER = [0.03, 0.06, 0.1, 0.4, 0.8]  # first levels a few ticks away, so the market can reach them during the test


async def case_tp_sl(b: BotSession, case: Case, side: str, finish: str) -> None:
    opened = await b.open_trade(case, side, FAR_LADDER, 0.9)
    if not opened:
        return
    trade, plan = opened
    await b.verify_protection(case, trade, plan)
    if finish == "modify_sl":  # move the stop, then close
        long = side == "BUY"
        new_stop = b.v.px(b.coin, plan["touch"] * (1 - (1 if long else -1) * 0.6 / 100))
        result = await b.command(case, "modify_sl", {"trade_id": trade["id"], "price": new_stop})
        case.check("bot accepted the stop-loss move", bool(result.get("ok")), True, result)
        moved = None
        for _ in range(20):
            moved = [o for o in await b.v.open_orders(b.coin) if o.is_trigger]
            if len(moved) == 1 and same(moved[0].trigger_px, new_stop):
                break
            await asyncio.sleep(0.5)
        case.check("exactly one stop on the exchange, at the new price, still full size",
                   bool(moved) and len(moved) == 1 and same(moved[0].trigger_px, new_stop) and same(moved[0].size, b.size), {"trigger": new_stop, "size": b.size}, moved)
        after = await b.trade(trade["id"])
        case.check("trade is still PROTECTED with the new stop recorded", bool(after) and after["state"] == "PROTECTED" and same(after["sl_price"], new_stop), new_stop,
                   {"state": after["state"], "sl_price": after["sl_price"]} if after else None)
    if finish == "kill":
        result = await b.command(case, "kill", {"confirm": "KILL"})
        case.check("kill switch reports nothing left open", result.get("ok") is True and result.get("remaining") == [], [], result.get("remaining"))
        await b.verify_closed(case, trade["id"])
        health = (await b.status())["health"]
        case.check("bot is paused after the kill switch", health["state"] == "PAUSED", "PAUSED", health["state"])
        result = await b.command(case, "resume", {})
        case.check("resume returns the bot to HEALTHY", result.get("state") == "HEALTHY", "HEALTHY", result.get("state"))
    else:
        result = await b.command(case, "close", {"trade_id": trade["id"]})
        case.check("close command accepted", bool(result.get("ok")), True, result)
        await b.verify_closed(case, trade["id"])


async def case_partial(b: BotSession, case: Case, side: str) -> None:
    """Several take-profit levels close the position in parts. The test cannot move the market, so the first levels
    sit a few ticks from the entry and the case waits for the market to trade through them."""
    opened = await b.open_trade(case, side, NEAR_LADDER, 0.7)
    if not opened:
        return
    trade, plan = opened
    wait = b.cfg["tp_wait_s"]
    case.step(f"waiting up to {wait} s for the market to reach a take-profit level")
    moved = await b.wait_trade(trade["id"], lambda t: t["state"] in ("CLOSED", "HALTED_MANUAL") or (t["remaining_size"] or 0) < (t["filled_size"] or 0) - 1e-12, wait)
    took = lambda t: [o for o in t["orders"] if o["role"] == "TP" and (o["filled_size"] or 0) > 0]  # noqa: E731  take-profit legs with any fill
    filled_tps = took(moved or trade)
    if not moved or (moved["state"] != "CLOSED" and not filled_tps):
        case.block(f"the market did not reach the first take-profit ({plan['take_profits'][0]}) within {wait} s, so partial closure could not be observed")
    elif moved["state"] == "CLOSED" and not filled_tps:
        case.block(f"the stop-loss was reached before any take-profit (outcome {moved.get('outcome')}); partial closure could not be observed")
        case.notes.append("the stop-loss execution itself is recorded in this case's orders")
    else:
        await asyncio.sleep(2.5)  # let the bot resize the stop after the fill
        now = await b.trade(trade["id"])
        filled_tps = took(now)
        done = [o for o in filled_tps if o["state"] == "FILLED"]
        closed_part = round(sum(o["filled_size"] for o in filled_tps), 10)
        case.step("take-profit filled", legs=[o["leg"] for o in filled_tps], closed=closed_part, trade_state=now["state"], remaining=now["remaining_size"])
        case.check("at least one take-profit level filled", len(filled_tps) >= 1, ">= 1", len(filled_tps))
        if now["state"] != "CLOSED":
            case.check("trade moved to REDUCING", now["state"] == "REDUCING", "REDUCING", now["state"])
            case.check("remaining size is the filled size minus what the take-profits closed", same(now["remaining_size"], b.size - closed_part, 1e-9), round(b.size - closed_part, 10),
                       now["remaining_size"])
            szi, orders = await b.v.szi(b.coin), await b.v.open_orders(b.coin)
            case.step("exchange after the partial close", position=szi, orders=orders)
            case.check("exchange position shrank to the remaining size", same(abs(szi), now["remaining_size"], 1e-9), now["remaining_size"], abs(szi))
            stops = [o for o in orders if o.is_trigger]
            case.check("stop-loss was resized to the remaining position", len(stops) == 1 and same(stops[0].size, now["remaining_size"], 1e-9), now["remaining_size"],
                       [o.size for o in stops])
            resting = [o for o in orders if not o.is_trigger]
            case.check("the unfilled take-profit levels still rest", len(resting) == b.tp_levels - len(done), b.tp_levels - len(done), len(resting))
            case.check("no more take-profit size rests than the position holds", sum(o.size for o in resting) <= abs(szi) + 1e-9, f"<= {abs(szi)}", round(sum(o.size for o in resting), 10))
        else:
            case.notes.append(f"the whole ladder (or the stop) completed before the check: outcome {now.get('outcome')}")
    current = await b.trade(trade["id"])
    if current and current["state"] != "CLOSED":
        result = await b.command(case, "close", {"trade_id": trade["id"]})
        case.check("close command for the rest accepted", bool(result.get("ok")), True, result)
    final = await b.verify_closed(case, trade["id"])
    if final and filled_tps:
        case.check("outcome records a partial win or better", final.get("outcome") in ("WIN_PARTIAL", "WIN_ALL", "MANUAL", "FLATTENED"), "a closed outcome", final.get("outcome"))


async def subtest_invalid_signal(b: BotSession, case: Case) -> None:
    """Rejection handling in the application: a malformed signal must not produce an order."""
    quote = await b.v.a.get_book(b.coin)
    price = b.v.px(b.coin, quote.ask)
    bad = f"#{b.coin} BUY NOW\n{b.coin} buy Zone {price:.10g} - {price:.10g}\n\nSL : {b.v.px(b.coin, price * 1.01):.10g}\n\nTP1 : {b.v.px(b.coin, price * 1.005):.10g}\n#QA_Signals"
    before = len(await b.v.open_orders(b.coin))
    result = await b.command(case, "inject", {"text": bad, "channel": "qa"})
    case.check("j. bot: a buy signal with its stop above the entry is rejected as INVALID", result.get("state") == "INVALID", "INVALID", {"state": result.get("state"), "reason": result.get("reason")})
    await asyncio.sleep(2)
    case.check("j. bot: the invalid signal produced no order and no position", len(await b.v.open_orders(b.coin)) == before and abs(await b.v.szi(b.coin)) < 1e-12, "nothing",
               {"open_orders": len(await b.v.open_orders(b.coin)), "position": await b.v.szi(b.coin)})


# ====================================================================== plan, run, report
def plan_cases() -> list[Case]:
    c = Case
    return [
        c("TC01", "Market buy", "BTC", "exchange", "Market buy BTC", "IOC buy at the ask with a slippage cap fills in full; position opens long; then closed"),
        c("TC02", "Market buy", "ETH", "exchange", "Market buy ETH", "as TC01"),
        c("TC03", "Market buy", "SOL", "exchange", "Market buy SOL", "as TC01"),
        c("TC04", "Market sell", "BTC", "exchange", "Market sell BTC (open short)", "IOC sell at the bid with a slippage cap fills in full; position opens short; then closed"),
        c("TC05", "Market sell", "ETH", "exchange", "Market sell ETH (open short)", "as TC04"),
        c("TC06", "Market sell", "SOL", "exchange", "Market sell SOL (open short)", "as TC04"),
        c("TC07", "Limit buy", "BTC", "exchange", "Limit buy BTC, resting below the market", "order rests (open), creates no position, cancels cleanly"),
        c("TC08", "Limit buy", "ETH", "exchange", "Limit buy ETH at the touch", "fills at the limit or better; position opens; then closed"),
        c("TC09", "Limit buy", "SOL", "exchange", "Limit buy SOL, post-only", "post-only order rests, creates no position, cancels cleanly"),
        c("TC10", "Limit sell", "BTC", "exchange", "Limit sell BTC at the touch", "fills at the limit or better; short opens; then closed"),
        c("TC11", "Limit sell", "ETH", "exchange", "Limit sell ETH, resting above the market", "order rests (open), creates no position, cancels cleanly"),
        c("TC12", "Limit sell", "SOL", "exchange", "Limit sell SOL, post-only", "post-only order rests, creates no position, cancels cleanly"),
        c("TC13", "Long with TP/SL", "BTC", "bot", "Bot: long BTC from a signal, closed by command", "IOC entry, one full-size reduce-only stop, five reduce-only take-profits, PROTECTED; close flattens and cancels"),
        c("TC14", "Long with TP/SL", "ETH", "bot", "Bot: long ETH, stop-loss moved, then closed", "as TC13, and moving the stop changes the exchange order"),
        c("TC15", "Short with TP/SL", "SOL", "bot", "Bot: short SOL from a signal, closed by command", "as TC13 on the short side"),
        c("TC16", "Short with TP/SL", "BTC", "bot", "Bot: short BTC, closed by the kill switch", "as TC15; kill flattens, cancels and pauses; resume restores"),
        c("TC17", "Multiple TP / partial close", "BTC", "bot", "Bot: long BTC, take-profit levels close the position in parts", "a filled level reduces the position, the stop is resized, the rest keeps resting"),
        c("TC18", "Multiple TP / partial close", "ETH", "bot", "Bot: short ETH, take-profit levels close the position in parts", "as TC17 on the short side"),
        c("TC19", "Cancel and replace", "BTC", "exchange", "Modify in place, cancel, and replace a resting order", "modify keeps the client id with the new price; cancel is final; the replacement rests alone"),
        c("TC20", "Invalid parameters", "ETH", "exchange+bot", "Rejected orders: nine exchange subtests and one application subtest", "every invalid order is rejected and none becomes a position"),
    ]


BTC20 = (  # (side, how the trade ends): ten longs and ten shorts
    ("BUY", "close"), ("SELL", "close"), ("BUY", "close"), ("SELL", "close"), ("BUY", "modify_sl"), ("SELL", "modify_sl"), ("BUY", "close"), ("SELL", "close"),
    ("BUY", "partial"), ("SELL", "partial"), ("BUY", "close"), ("SELL", "close"), ("BUY", "kill"), ("SELL", "kill"), ("BUY", "close"), ("SELL", "close"),
    ("BUY", "modify_sl"), ("SELL", "modify_sl"), ("BUY", "close"), ("SELL", "close"),
)
ENDING = {"close": "closed by command", "modify_sl": "stop-loss moved, then closed by command", "kill": "closed by the kill switch, then resumed",
          "partial": "take-profit levels a few ticks away; waits for a partial close, then closes the rest"}


def plan_btc20() -> list[Case]:
    cases = []
    for i, (side, ending) in enumerate(BTC20, 1):
        direction = "long" if side == "BUY" else "short"
        scenario = {"close": f"{direction.capitalize()} with TP/SL", "modify_sl": "Stop-loss moved", "kill": "Kill switch", "partial": "Multiple TP / partial close"}[ending]
        cases.append(Case(f"BT{i:02d}", scenario, "BTC", "bot", f"Bot: {direction} BTC, {ENDING[ending]}",
                          "IOC entry in the signal zone, one full-size reduce-only stop, five reduce-only take-profits, PROTECTED; the ending flattens and cancels everything"))
    return cases


async def run_case(case: Case, body: Any, v: Venue, results_path: Path, all_cases: list[Case], meta: dict[str, Any]) -> None:
    case.started_at = now_iso()
    log.info("%s | START | %s [%s, %s]", case.id, case.title, case.symbol, case.layer)
    try:
        await body
    except Exception as exc:  # noqa: BLE001  a crash is a failed case, and the run goes on
        case.error = f"{type(exc).__name__}: {str(exc)[:400]}"
        log.exception("%s | ERROR", case.id)
    finally:
        try:
            if not case.final_state:
                case.final_state = {"position": await v.szi(case.symbol), "open_orders": len(await v.open_orders(case.symbol))}
        except Exception:  # noqa: BLE001
            pass
        case.finish()
        write_results(results_path, all_cases, meta)  # after every case: nothing is lost if the run stops


def write_results(path: Path, cases: list[Case], meta: dict[str, Any]) -> None:
    counts = {s: sum(1 for c in cases if c.status == s) for s in ("passed", "failed", "blocked", "skipped", "not_run")}
    path.write_text(json.dumps({**meta, "counts": {"planned": len(cases), "executed": len(cases) - counts["not_run"] - counts["skipped"], **counts},
                                "cases": [c.record() for c in cases]}, indent=2, default=str))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="the operator's settings (account and key file are taken from it; nothing else)")
    parser.add_argument("--config", default=str(Path(__file__).with_name("qa_config.json")))
    parser.add_argument("--preflight", action="store_true", help="run the safety checks and print the plan; place no orders")
    parser.add_argument("--out", default=None, help="results folder (default: qa/results/<run id>)")
    parser.add_argument("--suite", choices=("matrix", "btc20", "live20"), default="matrix",
                        help="matrix: the 20-case scenario matrix; btc20: 20 trades on BTC through a test bot; live20: 20 trades on BTC through the operator's running bot")
    parser.add_argument("--live-size", type=float, default=None, help="live20: the size the running bot is configured to trade")
    parser.add_argument("--cases", default=None, help="comma-separated case ids to run (a retry: reported separately, never replaces a recorded result)")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.out) if args.out else Path(__file__).with_name("results") / run_id
    (run_dir / "bot").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(run_dir / "execution.log"), logging.StreamHandler(sys.stdout)])
    logging.Formatter.converter = time.gmtime

    # ---------------------------------------------------------------- safety: testnet only, alone on the account
    operator = parse_env_file(args.env_file)
    base_env = {k: operator[k] for k in ("HL_ACCOUNT_ADDRESS", "HL_AGENT_KEY_FILE") if operator.get(k)}
    if operator.get("HL_VAULT_ADDRESS"):
        base_env["HL_VAULT_ADDRESS"] = operator["HL_VAULT_ADDRESS"]
    direct_env = run_dir / "direct.env"
    direct_env.write_text("".join(f"{k}={v}\n" for k, v in {**base_env, "HL_ENV": "testnet", "HL_SYMBOL_MAP": json.dumps({s: s for s in cfg["symbols"]}),
                                                           "TELEGRAM_ENABLED": "false", "TELEGRAM_NOTIFY_ENABLED": "false", "DATA_DIR": str(run_dir / "bot" / "data"),
                                                           "LOG_DIR": str(run_dir / "bot" / "logs"), "API_ENABLED": "false"}.items()))
    settings = load_settings(environ={}, env_file=str(direct_env))
    isolation = {"env": settings.env, "rest_url": settings.rest_url, "ws_url": settings.ws_url, "operator_env": operator.get("HL_ENV")}
    log.info("isolation check: %s", isolation)
    if settings.env != "testnet" or TESTNET_MARK not in settings.rest_url or TESTNET_MARK not in settings.ws_url or operator.get("HL_ENV") != "testnet":
        log.error("STOP: testnet isolation cannot be verified. Nothing was sent.")
        return 2
    operator_settings = load_settings(environ={}, env_file=args.env_file)
    try:
        pid = int(operator_settings.pid_path.read_text().strip())
        os.kill(pid, 0)
        running = True
    except (OSError, ValueError):
        running = False
    if args.suite == "live20" and (not running or operator_settings.env != "testnet" or args.live_size is None):
        log.error("STOP: live20 needs the operator's bot running on testnet and --live-size. Nothing was sent.")
        return 2
    if args.suite != "live20" and running:
        log.error("STOP: the operator's bot is running on this account. Stop it first: two writers on one account is unsafe.")
        return 2

    adapter = build_adapter(settings, None, Clock())  # type: ignore[arg-type]
    v = Venue(adapter, run_id, cfg)
    v.instruments = await adapter.get_instruments()
    signer = await adapter.verify_signer()
    account = await adapter.get_account()
    limits = await adapter.get_limits()
    opened = await adapter.get_open_orders()
    markets = {}
    for coin in cfg["symbols"]:
        inst, quote = v.instruments.get(coin), await adapter.get_book(coin)
        markets[coin] = {"instrument": plain(inst), "bid": quote.bid, "ask": quote.ask, "spread_bps": round((quote.ask - quote.bid) / quote.bid * 1e4, 2),
                         "direct_size": v.size_for(coin, quote.ask, cfg["direct_notional_usd"]), "position": account.szi(coin),
                         "open_orders": sum(1 for o in opened if o.coin == coin)}
    equity = sum(a.account_value for a in account.by_dex.values())
    meta: dict[str, Any] = {
        "run_id": run_id, "started_at": now_iso(), "exchange": "Hyperliquid", "environment": "testnet", "isolation": isolation,
        "account": settings.account_address[:6] + "…" + settings.account_address[-4:], "signer_authorized": signer.authorized, "config": cfg,
        "start": {"account_value_usdc": round(equity, 4), "taker_fee": limits.taker_fee, "maker_fee": limits.maker_fee, "requests_used": limits.requests_used,
                  "requests_cap": limits.requests_cap, "markets": markets},
    }
    log.info("preflight: %s", json.dumps(meta["start"], default=str))
    problems = []
    if not signer.authorized:
        problems.append(f"the signing key is not authorised for the account: {signer.detail}")
    if equity < cfg["min_account_value_usdc"]:
        problems.append(f"account value {equity:.2f} USDC is below the configured minimum {cfg['min_account_value_usdc']}")
    for coin, m in markets.items():
        if m["instrument"] is None or m["instrument"]["is_delisted"]:
            problems.append(f"{coin} is not tradable on this testnet")
        if abs(m["position"]) > 1e-12 or m["open_orders"]:
            problems.append(f"{coin} is not flat at the start (position {m['position']}, {m['open_orders']} open orders): cleanup could not tell test orders from yours")
    if problems:
        for p in problems:
            log.error("STOP: %s", p)
        return 2
    cases = {"btc20": plan_btc20, "live20": plan_live20}.get(args.suite, plan_cases)()
    meta["suite"] = args.suite
    by_id = {c.id: c for c in cases}
    chosen = {c.strip().upper() for c in args.cases.split(",")} if args.cases else set(by_id)
    meta["retry_of"] = sorted(chosen) if args.cases else None
    results_path = run_dir / "results.json"
    write_results(results_path, cases, meta)
    if args.preflight:
        log.info("preflight passed: %d cases planned, no orders placed. Results folder: %s", len(cases), run_dir)
        return 0

    async def run(case_id: str, body: Any) -> None:
        if case_id in chosen:
            await run_case(by_id[case_id], body, v, results_path, cases, meta)
        else:
            body.close()  # not part of this run

    if args.suite == "live20":
        # ------------------------------------------------------------ 20 trades on BTC through the operator's own running bot
        cfg = {**cfg, "api_port": operator_settings.api_port, "bot_leverage": int(operator_settings.risk.leverage)}
        live = LiveBot(v, run_dir, base_env, cfg)
        live.data_dir = operator_settings.data_path
        live.tp_levels = len(operator_settings.tp_close_percents)
        meta["live_settings"] = {"tp_close_percents": list(operator_settings.tp_close_percents), "leverage": cfg["bot_leverage"], "size": args.live_size}
        day_limit = None
        try:
            attached = await live.attach("BTC", args.live_size)
            meta["bot_sessions"] = {"BTC": attached}
            day = attached["day"] or {}
            used = float(day.get("realized") or 0) - float(day.get("fees") or 0) + float(day.get("funding") or 0)
            day_limit = -float(operator_settings.risk.max_daily_loss_pct) / 100 * float(day.get("start_equity") or equity)
            need = cfg["live_est_cost_per_trade_usdc"] * len(cases)
            meta["day_budget"] = {"used_usdc": round(used, 4), "limit_usdc": round(day_limit, 4), "estimated_cost_usdc": round(need, 4)}
            log.info("daily-loss budget: %s", meta["day_budget"])
            if used - need <= day_limit:
                raise RuntimeError(f"the day's result is {used:.2f} USDC and the daily-loss limit is {day_limit:.2f}: {len(cases)} trades (about {need:.2f}) would trip it")
        except Exception as exc:  # noqa: BLE001
            log.error("live bot not usable: %s", exc)
            for case in cases:
                case.started_at = now_iso()
                case.block(f"{type(exc).__name__}: {str(exc)[:240]}")
                case.finish()
        else:
            for i, case in enumerate(cases):
                await run(case.id, case_live(live, case, "BUY" if i % 2 == 0 else "SELL"))
                status = await live.status()
                if status["open_trades"]:  # never leave a trade behind: ask the bot itself to close it
                    for t in status["open_trades"]:
                        await live.command(case, "close", {"trade_id": t["id"]})
                    await asyncio.sleep(6)
                    case.notes.append(f"trade(s) {[t['id'] for t in status['open_trades']]} were still open after the case; the bot was asked to close them")
                health = status["health"]
                if health["state"] != "HEALTHY":
                    case.notes.append(f"bot health after the case: {health['state']} {[(c['key'], c['reason'][:80]) for c in health['conditions']]}")
                    log.warning("bot health after %s: %s", case.id, case.notes[-1])
        finally:
            await live.stop()
        write_results(results_path, cases, meta)
    elif args.suite == "btc20":
        # ------------------------------------------------------------ 20 trades on BTC through the bot, one after the other
        bot = BotSession(v, run_dir, base_env, cfg)
        meta["bot_sessions"] = {}
        try:
            meta["bot_sessions"]["BTC"] = await bot.start("BTC")
        except Exception as exc:  # noqa: BLE001
            log.exception("the bot could not be started")
            for case in cases:
                case.started_at = now_iso()
                case.block(f"the bot could not be started: {type(exc).__name__}: {str(exc)[:200]}")
                case.finish()
            await bot.stop()
        else:
            try:
                for case, (side, ending) in zip(cases, BTC20, strict=True):
                    body = case_partial(bot, case, side) if ending == "partial" else case_tp_sl(bot, case, side, ending)
                    await run(case.id, body)
                    if abs(await v.szi("BTC")) > 1e-12:  # never start the next trade on top of a leftover position
                        case.notes.append(f"a position was left after the case and closed by the runner: {await v.sweep(None, 'BTC')}")
                    health = (await bot.status())["health"]
                    if health["state"] != "HEALTHY":  # e.g. left paused: say so and put it right, so one failure does not block the rest
                        case.notes.append(f"bot health after the case: {health['state']} {[c['key'] for c in health['conditions']]}")
                        await bot.command(case, "resume", {})
                        await asyncio.sleep(2)
            finally:
                await bot.stop()
        write_results(results_path, cases, meta)
    else:
        # ---------------------------------------------------------------- exchange layer
        for case_id, is_buy in (("TC01", True), ("TC02", True), ("TC03", True), ("TC04", False), ("TC05", False), ("TC06", False)):
            await run(case_id, case_market(v, by_id[case_id], is_buy))
        for case_id, is_buy, mode in (("TC07", True, "resting"), ("TC08", True, "marketable"), ("TC09", True, "post_only"),
                                      ("TC10", False, "marketable"), ("TC11", False, "resting"), ("TC12", False, "post_only")):
            await run(case_id, case_limit(v, by_id[case_id], is_buy, mode))
        await run("TC19", case_cancel_replace(v, by_id["TC19"]))
        tc20 = by_id["TC20"]
        if "TC20" in chosen:
            tc20.started_at = now_iso()
            try:
                await case_invalid(v, tc20)
            except Exception as exc:  # noqa: BLE001
                tc20.error = f"{type(exc).__name__}: {str(exc)[:400]}"
                log.exception("TC20 | ERROR")
        write_results(results_path, cases, meta)

        # ---------------------------------------------------------------- application layer: one bot session per symbol
        bot = BotSession(v, run_dir, base_env, cfg)
        sessions = (("BTC", (("TC13", case_tp_sl, ("BUY", "close")), ("TC16", case_tp_sl, ("SELL", "kill")), ("TC17", case_partial, ("BUY",)))),
                    ("ETH", (("TC14", case_tp_sl, ("BUY", "modify_sl")), ("TC18", case_partial, ("SELL",)))),
                    ("SOL", (("TC15", case_tp_sl, ("SELL", "close")),)))
        meta["bot_sessions"] = {}
        for coin, planned in sessions:
            planned = tuple(p for p in planned if p[0] in chosen)
            if not planned and not (coin == "ETH" and "TC20" in chosen):
                continue
            try:
                meta["bot_sessions"][coin] = await bot.start(coin)
            except Exception as exc:  # noqa: BLE001
                log.exception("bot session for %s failed to start", coin)
                for case_id, _fn, _a in planned:
                    by_id[case_id].started_at = now_iso()
                    by_id[case_id].block(f"the bot could not be started for {coin}: {type(exc).__name__}: {str(exc)[:200]}")
                    by_id[case_id].finish()
                await bot.stop()
                continue
            try:
                for case_id, fn, extra in planned:
                    await run(case_id, fn(bot, by_id[case_id], *extra))
                    leftovers = await v.sweep(None, coin) if (abs(await v.szi(coin)) > 1e-12) else None  # never start the next case on top of a leftover position
                    if leftovers:
                        by_id[case_id].notes.append(f"a position was left after the case and closed by the runner: {leftovers}")
                if coin == "ETH" and "TC20" in chosen and not tc20.error:
                    try:
                        await subtest_invalid_signal(bot, tc20)
                    except Exception as exc:  # noqa: BLE001
                        tc20.error = f"{type(exc).__name__}: {str(exc)[:400]}"
            finally:
                await bot.stop()
        if "TC20" in chosen:
            tc20.finish()
        write_results(results_path, cases, meta)

    # ---------------------------------------------------------------- final sweep and reconciliation
    cleanup: dict[str, Any] = {}
    for coin in cfg["symbols"]:
        if args.suite == "live20":  # read only: the running bot owns the account
            cleanup[coin] = {"position": await v.szi(coin), "open_orders": [plain(o) for o in await v.open_orders(coin)]}
        else:
            cleanup[coin] = await v.sweep(None, coin)
    await asyncio.sleep(1)
    account = await adapter.get_account()
    limits_end = await adapter.get_limits()
    remaining_orders = [plain(o) for o in await adapter.get_open_orders() if o.coin in cfg["symbols"]]
    end_equity = sum(a.account_value for a in account.by_dex.values())
    lat: dict[str, list[float]] = {}
    for entry in v.latencies:
        lat.setdefault(entry["kind"], []).append(entry["ms"])
    meta.update({
        "finished_at": now_iso(),
        "cleanup": {"clean": not remaining_orders and all(abs(account.szi(c)) < 1e-12 for c in cfg["symbols"]),
                    "positions": {c: account.szi(c) for c in cfg["symbols"]}, "open_orders": remaining_orders, "sweep": cleanup},
        "end": {"account_value_usdc": round(end_equity, 4), "account_value_change_usdc": round(end_equity - equity, 4), "requests_used": limits_end.requests_used},
        "read_failures": v.read_failures,
        "totals": {"fees_usdc": round(sum(c.fees for c in cases), 6), "realized_pnl_usdc": round(sum(c.realized_pnl for c in cases), 6)},
        "latency_ms": {kind: {"n": len(xs), "min": min(xs), "median": sorted(xs)[len(xs) // 2], "p95": sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.95))], "max": max(xs)}
                       for kind, xs in sorted(lat.items())},
    })
    write_results(results_path, cases, meta)
    log.info("finished: %s", json.dumps(json.loads(results_path.read_text())["counts"]))
    log.info("cleanup clean=%s positions=%s open_orders=%d", meta["cleanup"]["clean"], meta["cleanup"]["positions"], len(remaining_orders))
    return 0 if meta["cleanup"]["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
