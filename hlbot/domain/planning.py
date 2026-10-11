"""Pure planning helpers: content ids, the buffered entry range, expiry,
TP-ladder allocation, size splitting and the entry limit price.

Ported from the baseline ``app/execution_plan.py`` and re-based on
Hyperliquid's units: sizes are whole numbers of size increments
(``10 ** -szDecimals``) with a minimum notional per order instead of
lots with a minimum lot (EX-09 -> FR-040).

Every price distance here is in PRICE_POINTS: 1.00 of movement in the
quoted price (4373.25 -> 4374.25).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Optional

from hlbot.domain.rounding import round_price


def _fmt(value: float) -> str:
    return f"{value:.10g}"


def build_content_id(symbol: str, action: str, zone: dict[str, float], date_iso: Optional[str]) -> str:
    """Deterministic id for a signal's *content*:
    SYMBOL-ACTION-LOW-HIGH-YYYYMMDD, e.g. GOLD-SELL-4372-4375-20260922.
    A re-posted message for the same zone on the same UTC day maps to
    the same id, which is what duplicate protection keys on (FR-011)."""
    day = "".join(ch for ch in (date_iso or "")[:10] if ch.isdigit()) or "nodate"
    return f"{str(symbol).upper()}-{action}-{_fmt(zone['low'])}-{_fmt(zone['high'])}-{day}"


# Name used by the baseline; kept so its tests port unchanged.
build_signal_id = build_content_id


def compute_effective_range(zone: dict[str, float], buffer_points: float) -> dict[str, float]:
    """effective_lower = zone_low - buffer; effective_upper = zone_high + buffer."""
    return {
        "lower": round(zone["low"] - buffer_points, 10),
        "upper": round(zone["high"] + buffer_points, 10),
    }


def is_price_in_range(price: float, lower: float, upper: float) -> bool:
    return lower <= price <= upper


def executable_price(direction: str, bid: float, ask: float) -> float:
    """The price a marketable order trades against: BUY -> ask, SELL -> bid."""
    return ask if direction == "BUY" else bid


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def compute_expires_at(reference_iso: Optional[str], expiry_minutes: float) -> Optional[str]:
    """ISO timestamp ``expiry_minutes`` after ``reference_iso`` (the
    signal's Telegram timestamp), or None without a usable reference."""
    if not reference_iso:
        return None
    return (parse_iso(reference_iso) + timedelta(minutes=expiry_minutes)).isoformat()


def is_expired(expires_at_iso: Optional[str], now: datetime) -> bool:
    """A signal is not entered at or after its expiry instant (FR-020)."""
    return bool(expires_at_iso) and now >= parse_iso(expires_at_iso)  # type: ignore[arg-type]


def buffer_tp_price(direction: str, price: float, buffer_points: float) -> float:
    """Shift a take-profit ``buffer_points`` closer to entry (up for a
    SELL, down for a BUY) so the closing side of the book can reach it,
    at the cost of that many points of profit per filled leg (EX-13)."""
    sign = 1.0 if direction == "SELL" else -1.0
    return round(price + sign * buffer_points, 10)


def allocate_tp_levels(
    take_profits: dict[str, float],
    close_percents: list[float],
    direction: str = "SELL",
    tp_buffer_points: float = 0.0,
) -> list[dict]:
    """Pair each signal TP (tp1, tp2, ...) with the share of the position
    to close there. With fewer TPs than configured percentages, the
    leading percentages are rescaled to add up to 100; TPs beyond the
    configured percentages are not traded."""
    ordered = sorted(take_profits.items(), key=lambda item: int(item[0][2:]))
    count = min(len(ordered), len(close_percents))
    if count == 0:
        raise ValueError("no take-profit levels to allocate")

    percents = close_percents[:count]
    scale = 100.0 / sum(percents)
    return [
        {
            "level": int(key[2:]),
            "price": buffer_tp_price(direction, price, tp_buffer_points),
            "raw_price": price,
            "close_percent": round(percent * scale, 6),
        }
        for (key, price), percent in zip(ordered[:count], percents, strict=True)
    ]


def split_size_units(
    total_units: int, percents: Sequence[float], min_units: Sequence[int]
) -> Optional[list[int]]:
    """Split ``total_units`` size increments across TP legs (PRD §10.4):
    ``floor(F x w_i)`` per leg, the rounding remainder to TP1, and any
    leg below its minimum merged toward TP1.

    ``min_units[i]`` is the smallest order the exchange accepts at leg
    i's price (minimum notional). Returns one entry per leg (0 for a leg
    that was merged away) that always sums to ``total_units``, or None
    when the whole size cannot make even one valid order.
    """
    if len(percents) != len(min_units) or not percents:
        raise ValueError("percents and min_units must be the same non-zero length")
    if total_units <= 0:
        return None

    scale = sum(percents)
    units = [int(total_units * p / scale + 1e-9) for p in percents]
    units[0] += total_units - sum(units)

    legs = [0] * len(units)
    carry = 0
    for i in reversed(range(len(units))):
        merged = units[i] + carry
        if merged >= min_units[i] and merged > 0:
            legs[i] = merged
            carry = 0
        else:
            carry = merged
    if carry:
        # The legs nearest TP1 were too small on their own. Pull the
        # nearest surviving leg in to TP1 so the leftover closes at the
        # closest target instead of being pushed out to a further one.
        kept = next((i for i, u in enumerate(legs) if u), None)
        if kept is None:
            return None
        merged = legs[kept] + carry
        if merged >= min_units[0]:
            legs[kept] = 0
            legs[0] = merged
        else:
            legs[kept] = merged
    return legs


def entry_limit_price(
    direction: str,
    ref_price: float,
    max_slippage_points: float,
    tp1: float,
    min_tp_gap: float,
    sz_decimals: int,
) -> Optional[float]:
    """Limit price of the marketable IOC entry (PRD §10.2).

    BUY : min(ref + slippage, TP1 - gap) rounded DOWN to tick
    SELL: max(ref - slippage, TP1 + gap) rounded UP   to tick

    The clamp against TP1 guarantees no fill can land beyond the first
    target. Returns None when the clamp leaves the limit on the wrong
    side of ``ref`` (it could not cross the spread): the signal waits.
    """
    if direction == "BUY":
        limit = round_price(min(ref_price + max_slippage_points, tp1 - min_tp_gap), sz_decimals, "down")
        return limit if limit >= ref_price else None
    limit = round_price(max(ref_price - max_slippage_points, tp1 + min_tp_gap), sz_decimals, "up")
    return limit if limit <= ref_price else None


def adverse_slippage(direction: str, reference_price: float, fill_price: float) -> float:
    """Slippage in PRICE_POINTS, positive when the fill was worse than
    the reference (BUY filled higher / SELL filled lower)."""
    if direction == "BUY":
        return round(fill_price - reference_price, 10)
    return round(reference_price - fill_price, 10)


def validate_levels(
    direction: str, price: float, sl: float, tps: list[float], min_distance: float = 0.0
) -> list[str]:
    """Check SL and every TP sit on the correct side of the executable
    ``price`` by at least ``min_distance``. Returns a list of problems."""
    problems: list[str] = []
    if direction == "SELL":
        if sl < price + min_distance:
            problems.append(f"SL {sl:g} is not above entry price {price:g}")
        problems.extend(
            f"TP {tp:g} is not below entry price {price:g}" for tp in tps if tp > price - min_distance
        )
    else:
        if sl > price - min_distance:
            problems.append(f"SL {sl:g} is not below entry price {price:g}")
        problems.extend(
            f"TP {tp:g} is not above entry price {price:g}" for tp in tps if tp < price + min_distance
        )
    return problems


def apply_price_offset(parsed: dict, offset_points: float) -> dict:
    """Shift every price level of a parsed signal by ``offset_points``
    (EXEC_PRICE_OFFSET_POINTS, FR-025): the reference offset between the
    feed the signal was written against and the traded market."""
    if not offset_points:
        return parsed
    shifted = dict(parsed)
    zone = parsed["entry_zone"]
    shifted["entry_zone"] = {
        "low": round(zone["low"] + offset_points, 10),
        "high": round(zone["high"] + offset_points, 10),
    }
    shifted["stop_loss"] = round(parsed["stop_loss"] + offset_points, 10)
    shifted["take_profits"] = {
        key: round(value + offset_points, 10) for key, value in parsed["take_profits"].items()
    }
    return shifted
