"""Venue-neutral exchange adapter interface (PRD §13.1, NFR-016).

Strategy, risk and state logic depend only on this module. Nothing
outside ``hlbot/exchange/hyperliquid.py`` imports the Hyperliquid SDK.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Optional, Protocol


class ErrorClass(enum.StrEnum):
    """What an exchange error means for the caller (PRD §13.4)."""

    NO_MATCH = "no_match"  # IOC found nothing inside the limit: zero fill
    LOCAL_BUG = "local_bug"  # tick / minimum value: should be impossible
    INSUFFICIENT_MARGIN = "insufficient_margin"
    REDUCE_ONLY = "reduce_only"  # reduce-only would increase position
    INVALID_TPSL = "invalid_tpsl"  # trigger price already through the market
    NO_LIQUIDITY = "no_liquidity"
    OI_CAP = "oi_cap"
    ORACLE_FAR = "oracle_far"
    MARGIN_TIER = "margin_tier"
    NOT_FOUND = "not_found"  # cancel: never placed, already cancelled, or filled
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    EXPIRED = "expired"  # rejected because expiresAfter had passed
    UNKNOWN_OUTCOME = "unknown_outcome"  # timeout / 5xx / connection error
    OTHER = "other"


class ExchangeError(Exception):
    def __init__(self, kind: ErrorClass, text: str = ""):
        super().__init__(f"{kind.value}: {text}" if text else kind.value)
        self.kind = kind
        self.text = text


class OutcomeUnknown(ExchangeError):
    """The request may or may not have reached the exchange. Never retry
    blindly: resolve by client order id first (PRD §9.6.4)."""

    def __init__(self, text: str = ""):
        super().__init__(ErrorClass.UNKNOWN_OUTCOME, text)


@dataclass(frozen=True)
class Instrument:
    coin: str
    asset_id: int
    sz_decimals: int
    max_leverage: int
    dex: str = ""
    is_delisted: bool = False
    only_isolated: bool = False


@dataclass(frozen=True)
class Quote:
    coin: str
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    exchange_ms: Optional[int] = None
    received_ms: int = 0


@dataclass(frozen=True)
class MarketContext:
    coin: str
    mark: Optional[float]
    oracle: Optional[float]
    mid: Optional[float] = None
    funding: Optional[float] = None
    open_interest: Optional[float] = None
    received_ms: int = 0
    # Rolling 24-hour volume as the exchange reports it: in the coin (base) and in the collateral currency (quote).
    day_volume_base: Optional[float] = None
    day_volume_quote: Optional[float] = None
    prev_day_price: Optional[float] = None


@dataclass(frozen=True)
class Candle:
    """One candle. ``volume`` is in the coin (base volume); ``trades`` is
    the number of trades in the candle (tick volume)."""

    coin: str
    interval: str
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


@dataclass(frozen=True)
class Position:
    coin: str
    szi: float
    entry_px: Optional[float] = None
    unrealized_pnl: float = 0.0
    leverage: Optional[float] = None
    leverage_type: Optional[str] = None
    liquidation_px: Optional[float] = None
    margin_used: float = 0.0
    cum_funding: float = 0.0


@dataclass(frozen=True)
class DexAccount:
    dex: str
    account_value: float
    margin_used: float
    total_notional: float
    withdrawable: float
    maintenance_margin_used: float = 0.0

    @property
    def available_margin(self) -> float:
        return max(0.0, self.account_value - self.margin_used)


@dataclass(frozen=True)
class AccountState:
    by_dex: dict[str, DexAccount]
    positions: dict[str, Position]
    exchange_ms: Optional[int]
    received_ms: int
    # A unified account has one balance that backs every perp dex: ``by_dex`` then holds that same
    # balance under each managed dex (so the checks for a market find it), and it must be counted once.
    unified: bool = False

    def dex_account(self, dex: str) -> Optional[DexAccount]:
        return self.by_dex.get(dex)

    def accounts_once(self) -> dict[str, DexAccount]:
        """The balances, each counted once: for totals, snapshots and display."""
        if not self.unified or not self.by_dex:
            return self.by_dex
        first = sorted(self.by_dex)[0]
        return {first: self.by_dex[first]}

    def position(self, coin: str) -> Optional[Position]:
        return self.positions.get(coin)

    def szi(self, coin: str) -> float:
        position = self.positions.get(coin)
        return position.szi if position else 0.0


@dataclass(frozen=True)
class OpenOrder:
    coin: str
    oid: int
    cloid: Optional[str]
    is_buy: bool
    price: float
    size: float
    orig_size: float
    reduce_only: bool
    is_trigger: bool = False
    trigger_px: Optional[float] = None
    tpsl: Optional[str] = None  # "tp" | "sl" | None
    is_position_tpsl: bool = False
    order_type: str = ""
    timestamp: Optional[int] = None


@dataclass(frozen=True)
class OrderStatus:
    """Result of a lookup by client order id. ``found=False`` is the
    exchange's ``unknownOid``."""

    found: bool
    status: Optional[str] = None  # exchange status string, e.g. "open", "filled"
    oid: Optional[int] = None
    cloid: Optional[str] = None
    coin: Optional[str] = None
    size: Optional[float] = None  # remaining size
    orig_size: Optional[float] = None
    status_ms: Optional[int] = None

    @property
    def filled_size(self) -> Optional[float]:
        if self.size is None or self.orig_size is None:
            return None
        return max(0.0, round(self.orig_size - self.size, 10))


@dataclass(frozen=True)
class Fill:
    tid: int
    oid: int
    coin: str
    is_buy: bool
    price: float
    size: float
    time_ms: int
    fee: float = 0.0
    fee_token: Optional[str] = None
    closed_pnl: float = 0.0
    cloid: Optional[str] = None
    crossed: Optional[bool] = None
    dir: Optional[str] = None
    start_position: Optional[float] = None
    hash: Optional[str] = None
    liquidation: bool = False


@dataclass(frozen=True)
class OrderUpdate:
    oid: int
    cloid: Optional[str]
    coin: str
    status: str
    status_ms: Optional[int] = None
    size: Optional[float] = None
    orig_size: Optional[float] = None


@dataclass(frozen=True)
class FundingPayment:
    time_ms: int
    coin: str
    usdc: float
    rate: Optional[float] = None
    szi: Optional[float] = None


@dataclass(frozen=True)
class OrderRequest:
    """One order, venue-neutral. ``kind`` is ``limit`` or ``trigger``."""

    coin: str
    is_buy: bool
    size: float
    price: float
    reduce_only: bool
    cloid: str
    kind: str = "limit"
    tif: str = "Gtc"  # limit orders: Gtc | Ioc | Alo
    trigger_px: Optional[float] = None
    tpsl: Optional[str] = None  # trigger orders: "tp" | "sl"
    is_market: bool = True  # trigger orders: market or limit when triggered


@dataclass(frozen=True)
class PlaceResult:
    """Immediate (provisional) status of one order in a batch."""

    status: str  # "resting" | "filled" | "error"
    oid: Optional[int] = None
    total_size: Optional[float] = None
    avg_price: Optional[float] = None
    error: Optional[ExchangeError] = None


@dataclass(frozen=True)
class Limits:
    requests_used: Optional[int] = None
    requests_cap: Optional[int] = None
    taker_fee: Optional[float] = None
    maker_fee: Optional[float] = None

    @property
    def headroom(self) -> Optional[float]:
        if not self.requests_cap or self.requests_used is None:
            return None
        return max(0.0, 1.0 - self.requests_used / self.requests_cap)


@dataclass(frozen=True)
class SignerInfo:
    authorized: bool
    signer_address: Optional[str] = None
    expires_at_ms: Optional[int] = None
    detail: str = ""


@dataclass
class StreamHandlers:
    """Callbacks the adapter invokes from the event loop."""

    on_quote: Optional[Callable[[Quote], Awaitable[None]]] = None
    on_context: Optional[Callable[[MarketContext], Awaitable[None]]] = None
    on_fill: Optional[Callable[[Fill, bool], Awaitable[None]]] = None  # (fill, is_snapshot)
    on_order_update: Optional[Callable[[OrderUpdate], Awaitable[None]]] = None
    on_liquidation: Optional[Callable[[dict], Awaitable[None]]] = None
    on_funding: Optional[Callable[[FundingPayment], Awaitable[None]]] = None
    on_connection: Optional[Callable[[bool], Awaitable[None]]] = None  # connected / disconnected


@dataclass
class StreamHealth:
    connected: bool = False
    last_message_ms: int = 0
    reconnects: int = 0
    quote_ms: dict[str, int] = field(default_factory=dict)
    # Round-trip times, in milliseconds. None until measured (or when the
    # venue is simulated and there is no network to measure).
    ping_ms: Optional[float] = None  # stream: our ping -> the server's pong
    rest_ms: Optional[float] = None  # the most recent REST request
    rest_avg_ms: Optional[float] = None  # mean of the last 20 REST requests


class ExchangeAdapter(Protocol):
    """Everything the trading core may ask of a venue."""

    name: str

    # ---- market data
    async def get_instruments(self) -> dict[str, Instrument]: ...
    async def get_market(self, coin: str) -> MarketContext: ...
    async def get_book(self, coin: str) -> Quote: ...
    async def get_blocked_coins(self) -> list[str]: ...
    async def get_candles(self, coin: str, interval: str, count: int) -> list[Candle]: ...

    # ---- account
    async def get_account(self) -> AccountState: ...
    async def get_open_orders(self) -> list[OpenOrder]: ...
    async def get_order(self, cloid: str) -> OrderStatus: ...
    async def get_fills(self, since_ms: int) -> list[Fill]: ...
    async def get_funding(self, since_ms: int) -> list[FundingPayment]: ...
    async def get_limits(self) -> Limits: ...
    async def get_leverage(self, coin: str) -> Optional[int]: ...
    async def verify_signer(self) -> SignerInfo: ...

    # ---- signed actions (nonce issued by the action queue)
    async def set_leverage(self, coin: str, leverage: int, cross: bool, nonce: int) -> None: ...
    async def place(
        self, orders: Sequence[OrderRequest], nonce: int, expires_after: Optional[int] = None
    ) -> list[PlaceResult]: ...
    async def modify(self, cloid: str, new: OrderRequest, nonce: int) -> PlaceResult: ...
    async def cancel(self, coin: str, cloids: Sequence[str], nonce: int) -> list[Optional[ExchangeError]]: ...

    # ---- streams
    async def start(self, handlers: StreamHandlers) -> None: ...
    async def stop(self) -> None: ...
    def stream_health(self) -> StreamHealth: ...
    def rest_weight_last_minute(self) -> int: ...


def dex_of(coin: str) -> str:
    """Perp dex a coin belongs to: ``xyz:GOLD`` -> ``xyz``; ``PAXG`` -> ``""``."""
    return coin.split(":", 1)[0] if ":" in coin else ""
