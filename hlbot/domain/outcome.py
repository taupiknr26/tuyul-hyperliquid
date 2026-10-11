"""How a closed trade turned out (FR-056, FR-075).

The baseline classified legs from MT5 deal comments. Here the outcome
comes from *which orders produced the closing fills*: a fill is tagged
with the role of the order it belongs to (``TP``, ``SL``, ``CLOSE``) or
``None`` when no bot order produced it (a manual close on the exchange).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

WIN_ALL = "WIN_ALL"
WIN_PARTIAL = "WIN_PARTIAL"
LOSS_ALL = "LOSS_ALL"
MANUAL = "MANUAL"
FLATTENED = "FLATTENED"
LIQUIDATED = "LIQUIDATED"

# close_reason values that mean "the bot closed it to protect capital".
FLATTEN_REASONS = frozenset({"protection_failure", "kill", "sl_invalid", "too_small", "partial_fill_policy"})


def classify_outcome(closing_fills: Iterable[dict], close_reason: Optional[str] = None) -> str:
    """Outcome from the closing fills of one trade.

    Each fill is ``{"role": "TP"|"SL"|"CLOSE"|None, "liquidation": bool}``.
    Precedence: liquidation, then a bot flatten, then a manual close,
    then the TP/SL mix.
    """
    fills: list[dict] = list(closing_fills)
    if any(f.get("liquidation") for f in fills):
        return LIQUIDATED
    roles = [f.get("role") for f in fills]
    if "CLOSE" in roles:
        return FLATTENED if close_reason in FLATTEN_REASONS else MANUAL
    if not fills or any(role not in ("TP", "SL") for role in roles):
        return MANUAL
    if all(role == "TP" for role in roles):
        return WIN_ALL
    if "TP" not in roles:
        return LOSS_ALL
    return WIN_PARTIAL


def trade_pnl(fills: Iterable[dict], funding: float = 0.0) -> dict[str, float]:
    """``net = realised - fees + funding`` over every fill of the trade
    (entry fills carry fees and zero closed P/L)."""
    realized = 0.0
    fees = 0.0
    for fill in fills:
        realized += float(fill.get("closed_pnl") or 0.0)
        fees += float(fill.get("fee") or 0.0)
    return {
        "realized_pnl": round(realized, 6),
        "fees": round(fees, 6),
        "funding": round(funding, 6),
        "net_pnl": round(realized - fees + funding, 6),
    }
