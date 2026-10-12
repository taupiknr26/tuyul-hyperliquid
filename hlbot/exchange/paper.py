"""Paper adapter (FR-080): simulated execution against live market data.

Market data comes from a *read-only* Hyperliquid adapter built without a
signing key; orders, fills, positions and balances live entirely in the
simulator. Every validation and risk check runs exactly as in the other
modes (fixes LIM-15), and nothing here can reach ``/exchange``.

Simulated results are not execution results (PRD §16.1).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Optional

from hlbot.exchange.base import (
    AccountState,
    Candle,
    ExchangeAdapter,
    ExchangeError,
    Fill,
    FundingPayment,
    Instrument,
    Limits,
    MarketContext,
    OpenOrder,
    OrderRequest,
    OrderStatus,
    PlaceResult,
    Quote,
    SignerInfo,
    StreamHandlers,
    StreamHealth,
)
from hlbot.exchange.sim import SimExchange


class PaperAdapter:
    name = "paper"

    def __init__(
        self,
        market: ExchangeAdapter,
        sim: SimExchange,
        *,
        load_state: Optional[Callable[[], Optional[dict]]] = None,
        save_state: Optional[Callable[[dict], None]] = None,
    ):
        self.market = market
        self.sim = sim
        self._handlers = StreamHandlers()
        self._contexts: dict[str, MarketContext] = {}
        if load_state:
            saved = load_state()
            if saved:
                sim.restore(saved)
        if save_state:
            sim.on_change = lambda: save_state(sim.snapshot())

    # ---- market data: live
    async def get_instruments(self) -> dict[str, Instrument]:
        instruments = await self.market.get_instruments()
        self.sim.instruments = dict(instruments)
        return instruments

    async def get_market(self, coin: str) -> MarketContext:
        context = await self.market.get_market(coin)
        self._contexts[coin] = context
        await self._feed(coin, context=context)
        return context

    async def get_candles(self, coin: str, interval: str, count: int) -> list[Candle]:
        return await self.market.get_candles(coin, interval, count)

    async def get_book(self, coin: str) -> Quote:
        quote = await self.market.get_book(coin)
        await self._feed(coin, quote=quote)
        return quote

    async def get_blocked_coins(self) -> list[str]:
        return await self.market.get_blocked_coins()

    async def _feed(self, coin: str, quote: Optional[Quote] = None, context: Optional[MarketContext] = None) -> None:
        """Push live prices into the simulator so resting orders match."""
        if context is not None:
            self._contexts[coin] = context
        current = quote or self.sim.quotes.get(coin)
        if current is None or current.bid is None or current.ask is None:
            return
        ctx = self._contexts.get(coin)
        await self.sim.set_quote(
            coin, current.bid, current.ask, ctx.mark if ctx else None, ctx.oracle if ctx else None, emit=False
        )

    # ---- account and orders: simulated
    async def get_account(self) -> AccountState:
        return await self.sim.get_account()

    async def get_open_orders(self) -> list[OpenOrder]:
        return await self.sim.get_open_orders()

    async def get_order(self, cloid: str) -> OrderStatus:
        return await self.sim.get_order(cloid)

    async def get_fills(self, since_ms: int) -> list[Fill]:
        return await self.sim.get_fills(since_ms)

    async def get_funding(self, since_ms: int) -> list[FundingPayment]:
        return []

    async def get_limits(self) -> Limits:
        return await self.sim.get_limits()

    async def get_leverage(self, coin: str) -> Optional[int]:
        return await self.sim.get_leverage(coin)

    async def verify_signer(self) -> SignerInfo:
        return SignerInfo(True, None, None, "paper mode: no signing key is loaded")

    async def set_leverage(self, coin: str, leverage: int, cross: bool, nonce: int) -> None:
        await self.sim.set_leverage(coin, leverage, cross, nonce)

    async def place(
        self, orders: Sequence[OrderRequest], nonce: int, expires_after: Optional[int] = None
    ) -> list[PlaceResult]:
        return await self.sim.place(orders, nonce, expires_after)

    async def modify(self, cloid: str, new: OrderRequest, nonce: int) -> PlaceResult:
        return await self.sim.modify(cloid, new, nonce)

    async def cancel(self, coin: str, cloids: Sequence[str], nonce: int) -> list[Optional[ExchangeError]]:
        return await self.sim.cancel(coin, cloids, nonce)

    # ---- streams: live quotes drive the simulator, simulated fills drive the engine
    async def start(self, handlers: StreamHandlers) -> None:
        self._handlers = handlers
        await self.sim.start(
            StreamHandlers(on_fill=handlers.on_fill, on_order_update=handlers.on_order_update, on_liquidation=handlers.on_liquidation)
        )
        await self.market.start(
            StreamHandlers(on_quote=self._on_quote, on_context=self._on_context, on_connection=handlers.on_connection)
        )

    async def _on_quote(self, quote: Quote) -> None:
        await self._feed(quote.coin, quote=quote)
        if self._handlers.on_quote:
            await self._handlers.on_quote(quote)

    async def _on_context(self, context: MarketContext) -> None:
        await self._feed(context.coin, context=context)
        if self._handlers.on_context:
            await self._handlers.on_context(context)

    async def stop(self) -> None:
        await self.market.stop()
        await self.sim.stop()

    def stream_health(self) -> StreamHealth:
        return self.market.stream_health()

    def rest_weight_last_minute(self) -> int:
        return self.market.rest_weight_last_minute()
