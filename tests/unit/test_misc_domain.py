"""Cloids, outcome classification, error mapping, redaction."""

import pytest

from hlbot.domain.cloid import make_cloid, signal_key
from hlbot.domain.outcome import (
    FLATTENED,
    LIQUIDATED,
    LOSS_ALL,
    MANUAL,
    WIN_ALL,
    WIN_PARTIAL,
    classify_outcome,
    trade_pnl,
)
from hlbot.exchange.base import ErrorClass
from hlbot.exchange.errors import ENTRY_BLOCK_ASSET, ENTRY_REJECT_RISK, ENTRY_WAIT, classify_error
from hlbot.redact import clear_secrets, redact, redact_text, register_secrets


# ---------------------------------------------------------------- cloid (§9.6.4)
def test_cloid_is_deterministic_and_well_formed():
    key = signal_key(-1004307627275, 1477)
    a = make_cloid("testnet", "0xAbC" + "0" * 37, key, "ENTRY", 0, 1)
    assert a == make_cloid("testnet", "0xabc" + "0" * 37, key, "ENTRY", 0, 1)  # address case does not matter
    assert a.startswith("0x") and len(a) == 34 and int(a, 16) >= 0
    from hyperliquid.utils.types import Cloid

    Cloid(a)  # accepted by the SDK's validator


def test_cloid_changes_with_every_component():
    base = ("testnet", "0x" + "1" * 40, "-100:1", "ENTRY", 0, 1)
    seen = {make_cloid(*base)}
    for variant in (
        ("mainnet", *base[1:]), (base[0], "0x" + "2" * 40, *base[2:]), (*base[:2], "-100:2", *base[3:]),
        (*base[:3], "SL", 0, 1), (*base[:4], 1, 1), (*base[:5], 2), (*base[:2], "-200:1", *base[3:]),
    ):
        seen.add(make_cloid(*variant))
    assert len(seen) == 8  # TC-062: same message id in another channel is a different order identity


def test_cloid_rejects_unknown_role():
    with pytest.raises(ValueError):
        make_cloid("testnet", "0x" + "1" * 40, "k", "HEDGE", 0, 1)


# ---------------------------------------------------------------- outcome (FR-056; successor of the baseline's signal_outcome tests)
def fills(*roles, liquidation=False):
    return [{"role": role, "liquidation": liquidation} for role in roles]


def test_all_tps_is_win_all():
    assert classify_outcome(fills("TP", "TP", "TP")) == WIN_ALL
    assert classify_outcome(fills("TP")) == WIN_ALL


def test_tps_then_sl_is_win_partial():
    assert classify_outcome(fills("TP", "TP", "SL")) == WIN_PARTIAL


def test_sl_only_is_loss_all():
    assert classify_outcome(fills("SL")) == LOSS_ALL
    assert classify_outcome(fills("SL", "SL")) == LOSS_ALL


def test_a_fill_no_bot_order_produced_is_manual_not_a_loss():
    """Fixes LIM-11: a manual close is no longer classified as a loss."""
    assert classify_outcome(fills(None)) == MANUAL
    assert classify_outcome(fills("TP", None)) == MANUAL
    assert classify_outcome([]) == MANUAL


def test_bot_close_is_flattened_or_manual_by_reason():
    assert classify_outcome(fills("CLOSE"), "protection_failure") == FLATTENED
    assert classify_outcome(fills("TP", "CLOSE"), "kill") == FLATTENED
    assert classify_outcome(fills("CLOSE"), "manual") == MANUAL


def test_liquidation_takes_precedence():
    assert classify_outcome(fills("TP") + fills(None, liquidation=True)) == LIQUIDATED


def test_trade_pnl_is_realised_minus_fees_plus_funding_to_the_cent():
    result = trade_pnl(
        [{"closed_pnl": 0.0, "fee": 0.48}, {"closed_pnl": 12.5, "fee": 0.16}, {"closed_pnl": -3.25, "fee": 0.47}], funding=-0.07
    )
    assert result == {"realized_pnl": 9.25, "fees": 1.11, "funding": -0.07, "net_pnl": 8.07}


# ---------------------------------------------------------------- error mapping (§13.4, TC-085)
@pytest.mark.parametrize(
    "text, kind",
    [
        ("Order could not immediately match against any resting orders.", ErrorClass.NO_MATCH),
        ("Price must be divisible by tick size.", ErrorClass.LOCAL_BUG),
        ("Order must have minimum value of $10.", ErrorClass.LOCAL_BUG),
        ("Insufficient margin to place order.", ErrorClass.INSUFFICIENT_MARGIN),
        ("Reduce only order would increase position.", ErrorClass.REDUCE_ONLY),
        ("Invalid TP/SL price.", ErrorClass.INVALID_TPSL),
        ("No liquidity available for market order.", ErrorClass.NO_LIQUIDITY),
        ("Order would exceed open interest cap.", ErrorClass.OI_CAP),
        ("Order price too far from oracle", ErrorClass.ORACLE_FAR),
        ("Order would cause position to exceed margin tier limit at current leverage", ErrorClass.MARGIN_TIER),
        ("Order was never placed, already canceled, or filled.", ErrorClass.NOT_FOUND),
        ("User or API Wallet 0x1234 does not exist.", ErrorClass.AUTH),
        ("Must deposit before performing actions", ErrorClass.AUTH),
        ("Too many requests: rate limit exceeded (429).", ErrorClass.RATE_LIMIT),
        ("something nobody has seen before", ErrorClass.OTHER),
        ("", ErrorClass.OTHER),
    ],
)
def test_error_text_classification(text, kind):
    assert classify_error(text) == kind


def test_entry_error_classes_do_not_overlap():
    assert not (ENTRY_WAIT & ENTRY_REJECT_RISK) and not (ENTRY_WAIT & ENTRY_BLOCK_ASSET)
    assert ErrorClass.NO_MATCH in ENTRY_WAIT and ErrorClass.INSUFFICIENT_MARGIN in ENTRY_REJECT_RISK


# ---------------------------------------------------------------- redaction (SR-05)
def test_registered_secrets_and_patterns_are_removed():
    clear_secrets()
    register_secrets(["super-secret-api-hash-value"])
    key = "0x" + "ab" * 32
    token = "123456789:" + "A" * 35
    address = "0x" + "12" * 20
    text = redact_text(f"hash super-secret-api-hash-value key {key} token {token} addr {address}")
    assert "super-secret" not in text and key not in text and token not in text and address not in text
    assert "0x121212…" in text  # address keeps a short prefix only (NFR-022)
    clear_secrets()


def test_structured_redaction_keeps_tx_hashes_but_not_signatures():
    tx = "0x" + "cd" * 32
    data = redact({"hash": tx, "signature": {"r": "0x1", "s": "0x2", "v": 27}, "nested": [{"token": "abc"}, f"key {tx}"], "chat_id": 12345})
    assert data["hash"] == tx
    assert data["signature"] == "***" and data["nested"][0]["token"] == "***" and data["chat_id"] == "***"
    assert tx not in data["nested"][1]


# ---------------------------------------------------------------- wait_event (cancellation safety on every supported Python)
async def test_wait_event_returns_on_set_times_out_and_never_swallows_a_cancellation():
    import asyncio

    from hlbot.timeutil import wait_event

    event = asyncio.Event()
    assert await wait_event(event, 0.01) is False
    event.set()
    assert await wait_event(event, 0.01) is True and await wait_event(event, None) is True

    # A loop of the shape used throughout the bot must end when cancelled,
    # even when the cancellation lands exactly as the timeout fires.
    for _ in range(300):
        quiet = asyncio.Event()

        async def loop(quiet=quiet):
            while True:
                await wait_event(quiet, 0.001)

        task = asyncio.ensure_future(loop())
        await asyncio.sleep(0.001)
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=1.0)
        assert not pending, "a cancelled loop kept running"
        assert task.cancelled()
