"""Structural validation of a parsed signal (FR-010).

Ported unchanged from the baseline ``app/signal_validator.py``: SL and
every TP on the correct side of the zone, TPs monotonic, zone low < high.
A signal that carries parse errors is invalid immediately; incomplete
data is never repaired.
"""

from __future__ import annotations

from typing import Optional

VALIDATOR_VERSION = "1.0.0"


def _sorted_tp_values(take_profits: dict[str, float]) -> list[float]:
    return [take_profits[k] for k in sorted(take_profits, key=lambda k: int(k[2:]))]


def validate_signal(parsed: Optional[dict], parse_errors: list[str]) -> tuple[bool, list[str]]:
    """Validate a "signal" record's extracted fields. A non-empty
    `parse_errors` (from the parser) makes the signal invalid immediately —
    incomplete data is never guessed or repaired here."""
    if parse_errors:
        return False, [f"incomplete parse data: {err}" for err in parse_errors]

    errors: list[str] = []
    parsed = parsed or {}

    for field in ("symbol", "action", "entry_zone", "stop_loss", "take_profits"):
        if parsed.get(field) is None:
            errors.append(f"missing {field}")
    if errors:
        return False, errors

    action = parsed["action"]
    zone = parsed["entry_zone"]
    stop_loss = parsed["stop_loss"]
    take_profits = parsed["take_profits"]

    if action not in ("BUY", "SELL"):
        return False, [f"unknown action: {action!r}"]

    if zone["low"] >= zone["high"]:
        errors.append("entry_zone.low must be less than entry_zone.high")

    tp_values = _sorted_tp_values(take_profits)

    if action == "SELL":
        if stop_loss <= zone["high"]:
            errors.append("stop_loss must be above entry_zone for a SELL")
        if any(tp >= zone["low"] for tp in tp_values):
            errors.append("all take_profits must be below entry_zone for a SELL")
        if tp_values != sorted(tp_values, reverse=True):
            errors.append("take_profits must decrease from tp1 through tpN for a SELL")
    else:  # BUY
        if stop_loss >= zone["low"]:
            errors.append("stop_loss must be below entry_zone for a BUY")
        if any(tp <= zone["high"] for tp in tp_values):
            errors.append("all take_profits must be above entry_zone for a BUY")
        if tp_values != sorted(tp_values):
            errors.append("take_profits must increase from tp1 through tpN for a BUY")

    return (len(errors) == 0), errors


def build_validated_record(record: dict, valid: bool, errors: list[str], validated_at: str) -> dict:
    validated_record = dict(record)
    validated_record["valid"] = valid
    validated_record["validation_errors"] = errors
    validated_record["validator_version"] = VALIDATOR_VERSION
    validated_record["validated_at"] = validated_at
    return validated_record
