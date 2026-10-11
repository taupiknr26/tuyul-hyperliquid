"""Market volume for the dashboard's market card.

Three kinds of volume are kept apart, because the exchange reports them
differently:

* **base volume** - in the coin itself, exact, from the candles;
* **quote volume** - in the collateral currency. Exact for the rolling
  24 hours (the exchange reports it); for a single candle it is an
  estimate, base volume times the candle's typical price, and is marked
  as one;
* **tick volume** - the number of trades in a candle.

Read-only market data, fetched on demand and cached, so opening the page
in several browsers costs the exchange no more than one request per
symbol per cache period. Nothing here is used for trading decisions.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from hlbot.exchange.base import Candle, ExchangeError

if TYPE_CHECKING:  # pragma: no cover
    from hlbot.core.app import BotApp

INTERVALS_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DEFAULT_INTERVAL = "1h"
LOOKBACK = 20  # closed candles the average is taken over
CACHE_MS = 30_000
HIGH_RATIO, LOW_RATIO = 2.0, 0.5  # against the average: at or beyond these a candle is called unusually high / low
TREND_BAND = 0.10  # last closed candle within 10 % of the one before counts as flat
MIN_ELAPSED_FOR_PACE = 0.10  # do not extrapolate a candle that has only just opened


def _typical(candle: Candle) -> float:
    return (candle.high + candle.low + candle.close) / 3


def _brief(candle: Candle) -> dict[str, Any]:
    return {
        "open_ms": candle.open_ms,
        "close_ms": candle.close_ms,
        "volume_base": candle.volume,
        "volume_quote_est": round(candle.volume * _typical(candle), 2),
        "trades": candle.trades,
        "close": candle.close,
    }


def volume_summary(candles: list[Candle], now_ms: int, interval: str) -> dict[str, Any]:
    """What the market card shows for one symbol and interval. ``candles``
    are oldest first; the last may still be forming."""
    span = INTERVALS_MS[interval]
    ordered = sorted(candles, key=lambda c: c.open_ms)
    forming = ordered[-1] if ordered and ordered[-1].open_ms <= now_ms <= ordered[-1].close_ms else None
    closed = [c for c in ordered if c is not forming and c.close_ms < now_ms][-LOOKBACK:]
    result: dict[str, Any] = {"interval": interval, "current": None, "last_closed": None, "average": None, "vs_average_pct": None, "pace_vs_average_pct": None,
                              "trend": None, "level": None}
    if forming is not None:
        elapsed = min(1.0, max(0.0, (now_ms - forming.open_ms) / span))
        result["current"] = {**_brief(forming), "elapsed_pct": round(elapsed * 100, 1)}
    if closed:
        result["last_closed"] = _brief(closed[-1])
        mean = sum(c.volume for c in closed) / len(closed)
        result["average"] = {
            "candles": len(closed),
            "volume_base": mean,
            "volume_quote_est": round(sum(c.volume * _typical(c) for c in closed) / len(closed), 2),
            "trades": round(sum(c.trades for c in closed) / len(closed), 1),
        }
        if mean > 0:
            ratio = closed[-1].volume / mean
            result["vs_average_pct"] = round((ratio - 1) * 100, 1)
            result["level"] = "unusually_high" if ratio >= HIGH_RATIO else "unusually_low" if ratio <= LOW_RATIO else "normal"
            current = result["current"]
            if current and current["elapsed_pct"] >= MIN_ELAPSED_FOR_PACE * 100:
                pace = forming.volume / (current["elapsed_pct"] / 100) / mean  # type: ignore[union-attr]
                result["pace_vs_average_pct"] = round((pace - 1) * 100, 1)
        if len(closed) >= 2 and closed[-2].volume > 0:
            change = closed[-1].volume / closed[-2].volume - 1
            result["trend"] = "rising" if change > TREND_BAND else "falling" if change < -TREND_BAND else "flat"
            result["change_pct"] = round(change * 100, 1)
    return result


class VolumeService:
    """Per-symbol volume summaries with a short cache. A failed refresh
    keeps the last good value and marks it stale with the reason."""

    def __init__(self, bot: BotApp):
        self.bot = bot
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def _one(self, coin: str, interval: str, now_ms: int) -> dict[str, Any]:
        key = (coin, interval)
        cached = self._cache.get(key)
        if cached:
            if cached["error"]:  # a broken endpoint is not asked again by every page refresh
                if now_ms - cached["failed_ms"] < CACHE_MS // 3:
                    return cached
            elif now_ms - cached["fetched_ms"] < CACHE_MS:
                return cached
        try:
            try:
                candles = await self.bot.adapter.get_candles(coin, interval, LOOKBACK + 2)
            except ExchangeError:  # a read can simply be asked again; the first request after a quiet spell often times out
                candles = await self.bot.adapter.get_candles(coin, interval, LOOKBACK + 2)
            fresh = {"fetched_ms": now_ms, "summary": volume_summary(candles, now_ms, interval), "error": None}
        except ExchangeError as exc:  # keep the last good figures and say they are stale
            reason = "the exchange did not answer in time" if "timeout" in str(exc).lower() else str(exc)[:160]
            fresh = {"fetched_ms": cached["fetched_ms"] if cached else None, "summary": cached["summary"] if cached else None, "error": reason, "failed_ms": now_ms}
        self._cache[key] = fresh
        return fresh

    async def get(self, interval: Optional[str]) -> dict[str, Any]:
        interval = interval or DEFAULT_INTERVAL
        if interval not in INTERVALS_MS:
            raise ValueError(f"interval must be one of {', '.join(INTERVALS_MS)}")
        now_ms = self.bot.clock.now_ms()
        symbols: dict[str, Any] = {}
        async with self._lock:  # one refresh at a time, however many pages ask
            for coin in self.bot.s.coins:
                entry = await self._one(coin, interval, now_ms)
                context = self.bot.engine.contexts.get(coin)
                summary = entry["summary"]
                age_ms = now_ms - entry["fetched_ms"] if entry["fetched_ms"] else None
                symbols[coin] = {
                    **(summary or {"interval": interval, "current": None, "last_closed": None, "average": None, "vs_average_pct": None,
                                   "pace_vs_average_pct": None, "trend": None, "level": None}),
                    "base_unit": coin.split(":")[-1],
                    "day": {
                        "volume_base": context.day_volume_base if context else None,
                        "volume_quote": context.day_volume_quote if context else None,
                        "age_ms": now_ms - context.received_ms if context and context.received_ms else None,
                    },
                    "age_ms": age_ms,
                    "stale": bool(entry["error"]) or (age_ms is not None and age_ms > CACHE_MS * 4),
                    "error": entry["error"],
                }
        return {"interval": interval, "intervals": list(INTERVALS_MS), "lookback": LOOKBACK, "generated_at": self.bot.clock.iso(), "symbols": symbols}
