"""Hyperliquid price and size precision rules (FR-040, PRD §9.2).

Perp prices allow at most 5 significant figures and at most
``6 - szDecimals`` decimals; integer prices are always valid. Sizes are
multiples of ``10 ** -szDecimals``. Everything here is pure arithmetic
on ``Decimal`` so that 4191.3 never becomes 4191.299999.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

MAX_SIG_FIGS = 5
MAX_PRICE_DECIMALS_PERP = 6
MIN_ORDER_NOTIONAL = 10.0

_MODES = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_EVEN}
_EPS = 1e-9


def _d(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def price_decimals(price: float, sz_decimals: int) -> int:
    """Number of decimals a price near ``price`` may carry."""
    max_decimals = max(0, MAX_PRICE_DECIMALS_PERP - sz_decimals)
    d = _d(price)
    if d <= 0:
        return max_decimals
    integer_digits = d.adjusted() + 1  # 4191.3 -> 4, 0.05 -> -1
    return max(0, min(max_decimals, MAX_SIG_FIGS - integer_digits))


def tick_size(price: float, sz_decimals: int) -> float:
    return float(Decimal(1).scaleb(-price_decimals(price, sz_decimals)))


def round_price(price: float, sz_decimals: int, mode: str = "nearest") -> float:
    """Round to a valid tick. ``mode`` is ``down``, ``up`` or ``nearest``."""
    if mode not in _MODES:
        raise ValueError(f"unknown rounding mode: {mode!r}")
    quantum = Decimal(1).scaleb(-price_decimals(price, sz_decimals))
    return float(_d(price).quantize(quantum, rounding=_MODES[mode]))


def is_valid_price(price: float, sz_decimals: int) -> bool:
    if price <= 0:
        return False
    d = _d(price)
    if d == d.to_integral_value():
        return True
    return d == d.quantize(Decimal(1).scaleb(-price_decimals(price, sz_decimals)))


def size_step(sz_decimals: int) -> float:
    return float(Decimal(1).scaleb(-sz_decimals))


def size_units(size: float, sz_decimals: int) -> int:
    """``size`` as a whole number of size increments, rounded DOWN."""
    if size <= 0:
        return 0
    return int(size * (10**sz_decimals) + _EPS)


def units_to_size(units: int, sz_decimals: int) -> float:
    return float(Decimal(units).scaleb(-sz_decimals))


def floor_size(size: float, sz_decimals: int) -> float:
    """Round a size DOWN to the size increment; never rounds up (FR-030)."""
    return units_to_size(size_units(size, sz_decimals), sz_decimals)


def is_valid_size(size: float, sz_decimals: int) -> bool:
    if size <= 0:
        return False
    d = _d(size)
    return d == d.quantize(Decimal(1).scaleb(-sz_decimals))


def min_size_units(price: float, sz_decimals: int, min_notional: float = MIN_ORDER_NOTIONAL) -> int:
    """Smallest number of size increments whose notional at ``price`` is
    at least ``min_notional``."""
    if price <= 0:
        raise ValueError("price must be positive")
    scale = Decimal(10) ** sz_decimals
    raw = (_d(min_notional) / _d(price)) * scale
    return max(1, int(raw.to_integral_value(rounding=ROUND_CEILING)))


def round_toward_entry(price: float, sz_decimals: int, *, level_above_entry: bool) -> float:
    """Round a protective price to a valid tick *toward* the entry price:
    never widening risk and never moving a target further away (§10.2).
    A level above entry rounds down, a level below entry rounds up."""
    return round_price(price, sz_decimals, "down" if level_above_entry else "up")


def fmt_num(value: float) -> str:
    """Compact decimal string without float noise or trailing zeros."""
    text = format(_d(round(value, 10)).normalize(), "f")
    return "0" if text in ("-0", "") else text
