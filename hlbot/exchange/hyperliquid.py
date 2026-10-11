"""Hyperliquid adapter (PRD §9, §13).

The only module that imports the Hyperliquid SDK (NFR-016).

* Signing uses the official SDK's ``signing`` helpers unchanged; nothing
  about the signature scheme is re-implemented here (§9.6.2).
* The SDK's ``Exchange`` class is deliberately **not** used. It bundles
  withdrawal, transfer, agent-approval and sub-account funding calls,
  and it draws its own nonces. This adapter builds only four action
  types (``order``, ``batchModify``, ``cancelByCloid``,
  ``updateLeverage``) and takes every nonce from the action queue
  (SR-06, NFR-011).
* Asset ids are resolved at runtime from ``perpDexs`` + ``meta`` and are
  never hard-coded (FR-021).
* SDK helpers that pick a price implicitly (``market_open`` /
  ``market_close``) are not used; every order carries an explicit limit.

Without an agent key the adapter is read-only: paper mode uses it for
market data and never constructs a wallet.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests
from eth_account import Account
from hyperliquid.api import API
from hyperliquid.utils.error import ClientError, ServerError
from hyperliquid.utils.signing import (
    order_request_to_order_wire,
    order_wires_to_order_action,
    sign_l1_action,
)
from hyperliquid.utils.types import Cloid

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
from hlbot.exchange.errors import classify_error, to_error
from hlbot.exchange.hl_ws import HyperliquidWs
from hlbot.timeutil import Clock

logger = logging.getLogger("hlbot.exchange.hyperliquid")

# Info request weights (PRD §9.6.3).
_WEIGHT = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "spotClearinghouseState": 2,
    "orderStatus": 2,
    "userRole": 60,
}
_DEFAULT_WEIGHT = 20
_FILLS_PAGE = 2000


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def builder_dex_offset(dex_index: int) -> int:
    """Asset-id offset of a perp dex: 0 for the default dex, otherwise
    ``100000 + perp_dex_index x 10000`` (official asset-id rule)."""
    return 0 if dex_index == 0 else 100000 + dex_index * 10000


def parse_fill(raw: dict[str, Any]) -> Fill:
    return Fill(
        tid=int(raw["tid"]),
        oid=int(raw["oid"]),
        coin=str(raw["coin"]),
        is_buy=raw.get("side") == "B",
        price=float(raw["px"]),
        size=float(raw["sz"]),
        time_ms=int(raw["time"]),
        fee=_f(raw.get("fee")) or 0.0,
        fee_token=raw.get("feeToken"),
        closed_pnl=_f(raw.get("closedPnl")) or 0.0,
        cloid=raw.get("cloid"),
        crossed=raw.get("crossed"),
        dir=raw.get("dir"),
        start_position=_f(raw.get("startPosition")),
        hash=raw.get("hash"),
        liquidation=bool(raw.get("liquidation")),
    )


def parse_open_order(raw: dict[str, Any]) -> OpenOrder:
    order_type = str(raw.get("orderType") or "")
    is_trigger = bool(raw.get("isTrigger"))
    tpsl: Optional[str] = None
    if is_trigger:
        tpsl = "sl" if "stop" in order_type.lower() else "tp" if "take profit" in order_type.lower() else None
    return OpenOrder(
        coin=str(raw["coin"]),
        oid=int(raw["oid"]),
        cloid=raw.get("cloid"),
        is_buy=raw.get("side") == "B",
        price=float(raw["limitPx"]),
        size=float(raw["sz"]),
        orig_size=_f(raw.get("origSz")) or float(raw["sz"]),
        reduce_only=bool(raw.get("reduceOnly")),
        is_trigger=is_trigger,
        trigger_px=_f(raw.get("triggerPx")) if is_trigger else None,
        tpsl=tpsl,
        is_position_tpsl=bool(raw.get("isPositionTpsl")),
        order_type=order_type,
        timestamp=raw.get("timestamp"),
    )


def parse_order_status(raw: dict[str, Any]) -> OrderStatus:
    if not isinstance(raw, dict) or raw.get("status") != "order":
        return OrderStatus(found=False)
    wrapper = raw.get("order") or {}
    order = wrapper.get("order") or {}
    return OrderStatus(
        found=True,
        status=wrapper.get("status"),
        oid=order.get("oid"),
        cloid=order.get("cloid"),
        coin=order.get("coin"),
        size=_f(order.get("sz")),
        orig_size=_f(order.get("origSz")),
        status_ms=wrapper.get("statusTimestamp"),
    )


def parse_place_statuses(response: Any, expected: int) -> list[PlaceResult]:
    """``{"status": "ok", "response": {"data": {"statuses": [...]}}}`` ->
    one provisional result per order."""
    try:
        statuses = response["response"]["data"]["statuses"]
    except (KeyError, TypeError) as exc:
        raise OutcomeUnknown(f"unexpected order response shape: {str(response)[:200]}") from exc
    results: list[PlaceResult] = []
    for status in statuses:
        if isinstance(status, dict) and "resting" in status:
            results.append(PlaceResult("resting", oid=int(status["resting"]["oid"])))
        elif isinstance(status, dict) and "filled" in status:
            filled = status["filled"]
            results.append(
                PlaceResult("filled", oid=int(filled["oid"]), total_size=_f(filled.get("totalSz")), avg_price=_f(filled.get("avgPx")))
            )
        elif isinstance(status, dict) and "error" in status:
            results.append(PlaceResult("error", error=to_error(str(status["error"]))))
        else:
            results.append(PlaceResult("error", error=ExchangeError(ErrorClass.OTHER, f"unrecognised status {status!r}")))
    if len(results) != expected:
        # Pre-validation failures return a single error for the whole batch.
        if len(results) == 1 and results[0].status == "error":
            return results * expected
        raise OutcomeUnknown(f"expected {expected} order statuses, got {len(results)}")
    return results


UNIFIED_ACCOUNT = "unifiedAccount"
ABSTRACTION_TTL_MS = 60_000
COLLATERAL = "USDC"


def unified_accounts(by_dex: dict[str, DexAccount], positions: dict[str, Position], spot: dict[str, Any]) -> dict[str, DexAccount]:
    """One balance for every managed dex, from the spot state of a unified account.

    In this mode the per-dex clearinghouse states report an account value
    of zero and the collateral is the spot USDC balance (observed on
    mainnet with a flat account). Equity is that balance less any
    unrealised loss: an unrealised gain is deliberately not counted, so
    the figure the risk checks see is never too high. What is free is the
    balance less what is held or used as margin, and never more than the
    exchange's own "available after maintenance" figure."""
    usdc: dict[str, Any] = next((b for b in spot.get("balances") or [] if b.get("coin") == COLLATERAL), {})
    total, hold = _f(usdc.get("total")) or 0.0, _f(usdc.get("hold")) or 0.0
    after_maintenance = next(
        (_f(amount) for token, amount in spot.get("tokenToAvailableAfterMaintenance") or [] if token == usdc.get("token", 0)), None
    )
    equity = max(0.0, total + min(0.0, sum(p.unrealized_pnl for p in positions.values())))
    used = max(hold, sum(a.margin_used for a in by_dex.values()))
    free = max(0.0, equity - used)
    if after_maintenance is not None:
        free = min(free, max(0.0, after_maintenance))
    return {
        dex: DexAccount(
            dex=dex,
            account_value=equity,
            margin_used=equity - free,  # so that available_margin is exactly ``free``
            total_notional=sum(a.total_notional for a in by_dex.values()),
            withdrawable=free,
            maintenance_margin_used=sum(a.maintenance_margin_used for a in by_dex.values()),
        )
        for dex in by_dex
    }


CANDLE_INTERVALS_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def parse_context(coin: str, ctx: dict[str, Any], now_ms: int) -> MarketContext:
    """An asset context from REST (``metaAndAssetCtxs``) or the stream (``activeAssetCtx``): the same fields."""
    return MarketContext(
        coin=coin,
        mark=_f(ctx.get("markPx")),
        oracle=_f(ctx.get("oraclePx")),
        mid=_f(ctx.get("midPx")),
        funding=_f(ctx.get("funding")),
        open_interest=_f(ctx.get("openInterest")),
        received_ms=now_ms,
        day_volume_base=_f(ctx.get("dayBaseVlm")),
        day_volume_quote=_f(ctx.get("dayNtlVlm")),
        prev_day_price=_f(ctx.get("prevDayPx")),
    )


class HyperliquidAdapter:
    name = "hyperliquid"

    def __init__(
        self,
        *,
        rest_url: str,
        ws_url: str,
        is_mainnet: bool,
        coins: Sequence[str],
        account_address: Optional[str] = None,
        vault_address: Optional[str] = None,
        agent_key: Optional[str] = None,
        timeout_s: float = 5.0,
        ws_stale_ms: int = 10000,
        clock: Optional[Clock] = None,
    ):
        self._rest_url = rest_url
        self._ws_url = ws_url
        self._is_mainnet = is_mainnet
        self._coins = tuple(coins)
        self._dexes = tuple(dict.fromkeys(dex_of(c) for c in coins))
        self._account = account_address.lower() if account_address else None
        self._vault = vault_address.lower() if vault_address else None
        self._user = self._vault or self._account  # address whose state is queried
        self._wallet = Account.from_key(agent_key) if agent_key else None
        self._timeout = timeout_s
        self._ws_stale_ms = ws_stale_ms
        self.clock = clock or Clock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hl-rest")
        self._local = threading.local()
        self._weights: deque[tuple[float, int]] = deque()
        self._latencies: deque[float] = deque(maxlen=20)
        self._instruments: dict[str, Instrument] = {}
        self._abstraction_mode: Optional[str] = None
        self._abstraction_ms = 0
        self._handlers = StreamHandlers()
        self._ws: Optional[HyperliquidWs] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._health = StreamHealth()

    # ------------------------------------------------------------ transport
    def _api(self) -> API:
        api = getattr(self._local, "api", None)
        if api is None:
            api = API(self._rest_url, timeout=self._timeout)
            self._local.api = api
        return api

    def _post_sync(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            return self._api().post(path, payload)
        except ClientError as exc:
            text = str(exc.error_message or "")
            if exc.status_code == 429:
                raise ExchangeError(ErrorClass.RATE_LIMIT, text or "HTTP 429") from exc
            raise ExchangeError(classify_error(text), text or f"HTTP {exc.status_code}") from exc
        except ServerError as exc:
            raise OutcomeUnknown(f"HTTP {exc.status_code}") from exc
        except requests.RequestException as exc:
            raise OutcomeUnknown(type(exc).__name__) from exc

    def _account_weight(self, weight: int) -> None:
        now = time.monotonic()
        self._weights.append((now, weight))
        while self._weights and now - self._weights[0][0] > 60.0:
            self._weights.popleft()

    def rest_weight_last_minute(self) -> int:
        now = time.monotonic()
        return sum(w for t, w in self._weights if now - t <= 60.0)

    async def _timed(self, fn: Any, *args: Any) -> Any:
        """Run one blocking REST call on the pool and record how long the
        round trip took, failures and timeouts included."""
        started = time.monotonic()
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)
        finally:
            elapsed = round((time.monotonic() - started) * 1000.0, 1)
            self._latencies.append(elapsed)
            self._health.rest_ms = elapsed
            self._health.rest_avg_ms = round(sum(self._latencies) / len(self._latencies), 1)

    async def _info(self, payload: dict[str, Any]) -> Any:
        self._account_weight(_WEIGHT.get(str(payload.get("type")), _DEFAULT_WEIGHT))
        return await self._timed(self._post_sync, "/info", payload)

    def _require_user(self) -> str:
        if not self._user:
            raise ExchangeError(ErrorClass.AUTH, "no account address configured")
        return self._user

    # ------------------------------------------------------------ market data
    async def get_instruments(self) -> dict[str, Instrument]:
        dexs = await self._info({"type": "perpDexs"})
        names = [""] + [str(d["name"]) for d in dexs[1:] if d]
        instruments: dict[str, Instrument] = {}
        for dex in self._dexes:
            if dex not in names:
                logger.warning("perp dex %r is not listed", dex)
                continue
            offset = builder_dex_offset(names.index(dex))
            meta = await self._info({"type": "meta", "dex": dex})
            for index, asset in enumerate(meta["universe"]):
                coin = str(asset["name"])
                instruments[coin] = Instrument(
                    coin=coin,
                    asset_id=offset + index,
                    sz_decimals=int(asset["szDecimals"]),
                    max_leverage=int(asset["maxLeverage"]),
                    dex=dex,
                    is_delisted=bool(asset.get("isDelisted")),
                    only_isolated=bool(asset.get("onlyIsolated")) or asset.get("marginMode") in ("strictIsolated", "noCross"),
                )
        self._instruments = instruments
        return dict(instruments)

    async def get_market(self, coin: str) -> MarketContext:
        meta, contexts = await self._info({"type": "metaAndAssetCtxs", "dex": dex_of(coin)})
        for asset, ctx in zip(meta["universe"], contexts, strict=False):
            if asset["name"] == coin:
                return parse_context(coin, ctx, self.clock.now_ms())
        raise ExchangeError(ErrorClass.OTHER, f"{coin} not found in asset contexts")

    async def get_candles(self, coin: str, interval: str, count: int) -> list[Candle]:
        """The latest ``count`` candles, oldest first; the last one is still forming. Read-only market data."""
        span = CANDLE_INTERVALS_MS.get(interval)
        if span is None:
            raise ExchangeError(ErrorClass.LOCAL_BUG, f"unsupported candle interval {interval!r}")
        now = self.clock.now_ms()
        raw = await self._info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": now - span * (count + 1), "endTime": now}})
        candles = [
            Candle(coin=coin, interval=interval, open_ms=int(c["t"]), close_ms=int(c["T"]), open=float(c["o"]), high=float(c["h"]), low=float(c["l"]),
                   close=float(c["c"]), volume=float(c["v"]), trades=int(c.get("n") or 0))
            for c in raw or []
        ]
        return sorted(candles, key=lambda c: c.open_ms)[-count:]

    async def get_book(self, coin: str) -> Quote:
        book = await self._info({"type": "l2Book", "coin": coin})
        bids, asks = book["levels"][0], book["levels"][1]
        return Quote(
            coin=coin,
            bid=float(bids[0]["px"]) if bids else None,
            ask=float(asks[0]["px"]) if asks else None,
            bid_size=float(bids[0]["sz"]) if bids else None,
            ask_size=float(asks[0]["sz"]) if asks else None,
            exchange_ms=int(book["time"]) if book.get("time") else None,
            received_ms=self.clock.now_ms(),
        )

    async def get_blocked_coins(self) -> list[str]:
        blocked: list[str] = []
        for dex in self._dexes:
            blocked.extend(str(c) for c in await self._info({"type": "perpsAtOpenInterestCap", "dex": dex}))
        return blocked

    # ------------------------------------------------------------ account
    async def get_account(self) -> AccountState:
        user = self._require_user()
        by_dex: dict[str, DexAccount] = {}
        positions: dict[str, Position] = {}
        exchange_ms: Optional[int] = None
        for dex in self._dexes:
            state = await self._info({"type": "clearinghouseState", "user": user, "dex": dex})
            summary = state.get("marginSummary") or {}
            by_dex[dex] = DexAccount(
                dex=dex,
                account_value=_f(summary.get("accountValue")) or 0.0,
                margin_used=_f(summary.get("totalMarginUsed")) or 0.0,
                total_notional=_f(summary.get("totalNtlPos")) or 0.0,
                withdrawable=_f(state.get("withdrawable")) or 0.0,
                maintenance_margin_used=_f(state.get("crossMaintenanceMarginUsed")) or 0.0,
            )
            exchange_ms = int(state["time"]) if state.get("time") else exchange_ms
            for entry in state.get("assetPositions") or []:
                pos = entry.get("position") or {}
                szi = _f(pos.get("szi")) or 0.0
                if not szi:
                    continue
                leverage = pos.get("leverage") or {}
                funding = pos.get("cumFunding") or {}
                positions[str(pos["coin"])] = Position(
                    coin=str(pos["coin"]),
                    szi=szi,
                    entry_px=_f(pos.get("entryPx")),
                    unrealized_pnl=_f(pos.get("unrealizedPnl")) or 0.0,
                    leverage=_f(leverage.get("value")),
                    leverage_type=leverage.get("type"),
                    liquidation_px=_f(pos.get("liquidationPx")),
                    margin_used=_f(pos.get("marginUsed")) or 0.0,
                    cum_funding=_f(funding.get("sinceOpen")) or 0.0,
                )
        if await self._abstraction() == UNIFIED_ACCOUNT:
            spot = await self._info({"type": "spotClearinghouseState", "user": user})
            return AccountState(unified_accounts(by_dex, positions, spot), positions, exchange_ms, self.clock.now_ms(), unified=True)
        return AccountState(by_dex, positions, exchange_ms, self.clock.now_ms())

    async def _abstraction(self) -> str:
        """The account's abstraction mode, re-read once a minute (the owner can change it on the exchange).
        A failed lookup keeps the last known mode; with none known it reads as standard, where an
        unfunded dex blocks entries: the safe direction."""
        now = self.clock.now_ms()
        if self._abstraction_mode is None or now - self._abstraction_ms >= ABSTRACTION_TTL_MS:
            try:
                mode = await self._info({"type": "userAbstraction", "user": self._require_user()})
                self._abstraction_mode, self._abstraction_ms = str(mode), now
            except ExchangeError as exc:
                logger.warning("could not read the account's abstraction mode: %s", exc)
        return self._abstraction_mode or "disabled"

    async def get_open_orders(self) -> list[OpenOrder]:
        user = self._require_user()
        orders: list[OpenOrder] = []
        for dex in self._dexes:
            raw = await self._info({"type": "frontendOpenOrders", "user": user, "dex": dex})
            orders.extend(parse_open_order(o) for o in raw)
        return orders

    async def get_order(self, cloid: str) -> OrderStatus:
        return parse_order_status(await self._info({"type": "orderStatus", "user": self._require_user(), "oid": cloid}))

    async def get_fills(self, since_ms: int) -> list[Fill]:
        """Paged by time, de-duplicated by ``tid`` (HL-A06)."""
        user = self._require_user()
        seen: dict[int, Fill] = {}
        start = max(0, int(since_ms))
        for _ in range(10):  # 10 pages x 2,000 is the whole retained history
            page = await self._info({"type": "userFillsByTime", "user": user, "startTime": start, "aggregateByTime": False})
            for raw in page:
                if raw.get("coin") in self._coins:
                    fill = parse_fill(raw)
                    seen[fill.tid] = fill
            if len(page) < _FILLS_PAGE:
                break
            last = max(int(raw["time"]) for raw in page)
            if last <= start:
                start += 1
            else:
                start = last
        return sorted(seen.values(), key=lambda f: (f.time_ms, f.tid))

    async def get_funding(self, since_ms: int) -> list[FundingPayment]:
        raw = await self._info({"type": "userFunding", "user": self._require_user(), "startTime": max(0, int(since_ms))})
        payments: list[FundingPayment] = []
        for entry in raw:
            delta = entry.get("delta") or {}
            if delta.get("type") == "funding" and delta.get("coin") in self._coins:
                payments.append(
                    FundingPayment(
                        time_ms=int(entry["time"]),
                        coin=str(delta["coin"]),
                        usdc=_f(delta.get("usdc")) or 0.0,
                        rate=_f(delta.get("fundingRate")),
                        szi=_f(delta.get("szi")),
                    )
                )
        return payments

    async def get_limits(self) -> Limits:
        user = self._require_user()
        rate = await self._info({"type": "userRateLimit", "user": user})
        fees = await self._info({"type": "userFees", "user": user})
        return Limits(
            requests_used=rate.get("nRequestsUsed"),
            requests_cap=rate.get("nRequestsCap"),
            taker_fee=_f(fees.get("userCrossRate")),
            maker_fee=_f(fees.get("userAddRate")),
        )

    async def get_leverage(self, coin: str) -> Optional[int]:
        data = await self._info({"type": "activeAssetData", "user": self._require_user(), "coin": coin})
        value = (data.get("leverage") or {}).get("value")
        return int(value) if value is not None else None

    async def verify_signer(self) -> SignerInfo:
        """The signing key must be an agent approved by the account
        (FR-070). Its own address never holds funds (SR-01)."""
        if self._wallet is None:
            return SignerInfo(False, detail="no signing key loaded")
        signer = self._wallet.address.lower()
        if signer == self._account:
            return SignerInfo(False, signer, detail="the signing key is the account's master key; use an agent (API) wallet")
        role = await self._info({"type": "userRole", "user": signer})
        master = str(((role.get("data") or {}).get("user")) or "").lower()
        if role.get("role") != "agent" or master != self._account:
            return SignerInfo(False, signer, detail=f"signer role is {role.get('role')!r}, not an agent of the configured account")
        expires: Optional[int] = None
        try:
            for agent in await self._info({"type": "extraAgents", "user": self._account}):
                if str(agent.get("address", "")).lower() == signer and agent.get("validUntil"):
                    expires = int(agent["validUntil"])
        except ExchangeError:  # expiry is advisory; authorisation is already proven
            logger.warning("could not read agent expiry")
        return SignerInfo(True, signer, expires, "agent approved")

    # ------------------------------------------------------------ signed actions
    def _asset(self, coin: str) -> int:
        instrument = self._instruments.get(coin)
        if instrument is None:
            raise ExchangeError(ErrorClass.LOCAL_BUG, f"asset id for {coin} has not been resolved")
        return instrument.asset_id

    def _wire(self, order: OrderRequest) -> dict[str, Any]:
        if order.kind == "trigger":
            order_type: dict[str, Any] = {
                "trigger": {"triggerPx": order.trigger_px, "isMarket": order.is_market, "tpsl": order.tpsl}
            }
        else:
            order_type = {"limit": {"tif": order.tif}}
        return order_request_to_order_wire(
            {
                "coin": order.coin,
                "is_buy": order.is_buy,
                "sz": order.size,
                "limit_px": order.price,
                "order_type": order_type,
                "reduce_only": order.reduce_only,
                "cloid": Cloid(order.cloid),
            },
            self._asset(order.coin),
        )

    def sign(self, action: dict[str, Any], nonce: int, expires_after: Optional[int]) -> dict[str, Any]:
        if self._wallet is None:
            raise ExchangeError(ErrorClass.AUTH, "read-only adapter: no signing key loaded")
        return sign_l1_action(self._wallet, action, self._vault, nonce, expires_after, self._is_mainnet)

    def _exchange_sync(self, action: dict[str, Any], nonce: int, expires_after: Optional[int]) -> Any:
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": self.sign(action, nonce, expires_after),
            "vaultAddress": self._vault,
            "expiresAfter": expires_after,
        }
        response = self._post_sync("/exchange", payload)
        if isinstance(response, dict) and response.get("status") == "err":
            raise to_error(str(response.get("response")))
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise OutcomeUnknown(f"unexpected exchange response: {str(response)[:200]}")
        return response

    async def _exchange(self, action: dict[str, Any], nonce: int, expires_after: Optional[int] = None) -> Any:
        self._account_weight(1 + len(action.get("orders") or action.get("cancels") or action.get("modifies") or []) // 40)
        return await self._timed(self._exchange_sync, action, nonce, expires_after)

    async def set_leverage(self, coin: str, leverage: int, cross: bool, nonce: int) -> None:
        await self._exchange({"type": "updateLeverage", "asset": self._asset(coin), "isCross": cross, "leverage": int(leverage)}, nonce)

    async def place(
        self, orders: Sequence[OrderRequest], nonce: int, expires_after: Optional[int] = None
    ) -> list[PlaceResult]:
        action = order_wires_to_order_action([self._wire(o) for o in orders], None, "na")
        return parse_place_statuses(await self._exchange(action, nonce, expires_after), len(orders))

    async def modify(self, cloid: str, new: OrderRequest, nonce: int) -> PlaceResult:
        action = {"type": "batchModify", "modifies": [{"oid": Cloid(cloid).to_raw(), "order": self._wire(new)}]}
        return parse_place_statuses(await self._exchange(action, nonce), 1)[0]

    async def cancel(self, coin: str, cloids: Sequence[str], nonce: int) -> list[Optional[ExchangeError]]:
        asset = self._asset(coin)
        action = {"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": Cloid(c).to_raw()} for c in cloids]}
        response = await self._exchange(action, nonce)
        try:
            statuses = response["response"]["data"]["statuses"]
        except (KeyError, TypeError) as exc:
            raise OutcomeUnknown(f"unexpected cancel response shape: {str(response)[:200]}") from exc
        return [None if s == "success" else to_error(str(s.get("error") if isinstance(s, dict) else s)) for s in statuses]

    # ------------------------------------------------------------ streams
    async def start(self, handlers: StreamHandlers) -> None:
        self._handlers = handlers
        subscriptions: list[dict[str, Any]] = []
        for coin in self._coins:
            subscriptions.append({"type": "bbo", "coin": coin})
            subscriptions.append({"type": "activeAssetCtx", "coin": coin})
        if self._user:
            subscriptions.append({"type": "userFills", "user": self._user})
            subscriptions.append({"type": "orderUpdates", "user": self._user})
            subscriptions.append({"type": "userEvents", "user": self._user})
        self._ws = HyperliquidWs(
            self._ws_url,
            subscriptions,
            on_message=self._on_ws_message,
            on_connection=self._on_ws_connection,
            health=self._health,
            clock=self.clock,
            stale_ms=self._ws_stale_ms,
        )
        self._ws_task = asyncio.get_running_loop().create_task(self._ws.run(), name="hl-ws")

    async def stop(self) -> None:
        if self._ws:
            await self._ws.stop()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stream_health(self) -> StreamHealth:
        return self._health

    async def _on_ws_connection(self, connected: bool) -> None:
        if self._handlers.on_connection:
            await self._handlers.on_connection(connected)

    async def _on_ws_message(self, message: dict[str, Any]) -> None:
        channel = message.get("channel")
        data = message.get("data")
        h = self._handlers
        now = self.clock.now_ms()
        if channel == "bbo" and isinstance(data, dict):
            bid, ask = (data.get("bbo") or [None, None])[:2]
            coin = str(data.get("coin"))
            self._health.quote_ms[coin] = now
            if h.on_quote:
                await h.on_quote(
                    Quote(
                        coin=coin,
                        bid=float(bid["px"]) if bid else None,
                        ask=float(ask["px"]) if ask else None,
                        bid_size=float(bid["sz"]) if bid else None,
                        ask_size=float(ask["sz"]) if ask else None,
                        exchange_ms=data.get("time"),
                        received_ms=now,
                    )
                )
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            ctx = data.get("ctx") or {}
            if h.on_context:
                await h.on_context(parse_context(str(data.get("coin")), ctx, now))
        elif channel == "userFills" and isinstance(data, dict):
            snapshot = bool(data.get("isSnapshot"))
            for raw in data.get("fills") or []:
                if raw.get("coin") in self._coins and h.on_fill:
                    await h.on_fill(parse_fill(raw), snapshot)
        elif channel == "orderUpdates" and isinstance(data, list):
            for entry in data:
                order = entry.get("order") or {}
                if order.get("coin") in self._coins and h.on_order_update:
                    await h.on_order_update(
                        OrderUpdate(
                            oid=int(order["oid"]),
                            cloid=order.get("cloid"),
                            coin=str(order["coin"]),
                            status=str(entry.get("status")),
                            status_ms=entry.get("statusTimestamp"),
                            size=_f(order.get("sz")),
                            orig_size=_f(order.get("origSz")),
                        )
                    )
        elif channel == "user" and isinstance(data, dict):
            if data.get("liquidation") and h.on_liquidation:
                await h.on_liquidation(dict(data["liquidation"]))
            for raw in data.get("fills") or []:
                if raw.get("coin") in self._coins and h.on_fill:
                    await h.on_fill(parse_fill(raw), False)
            funding = data.get("funding")
            if funding and funding.get("coin") in self._coins and h.on_funding:
                await h.on_funding(
                    FundingPayment(
                        time_ms=int(funding.get("time") or now),
                        coin=str(funding["coin"]),
                        usdc=_f(funding.get("usdc")) or 0.0,
                        rate=_f(funding.get("fundingRate")),
                        szi=_f(funding.get("szi")),
                    )
                )
        elif channel == "error":
            logger.warning("websocket error message: %s", str(data)[:200])
