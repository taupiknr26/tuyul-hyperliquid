"""Protection protocol, partial fills, flatten (PRD §10.5; TC-020..TC-025,
TC-030..TC-032, TC-052)."""

import pytest

from hlbot.exchange.base import ErrorClass
from tests.component.conftest import COIN, SELL_TEXT


def is_sl(order):
    return order.kind == "trigger" and order.tpsl == "sl"


async def test_tc020_sl_is_verified_within_the_soft_deadline_and_time_to_protected_is_measured(h):
    trade = await h.open_protected_trade()
    assert trade["protected_at"] is not None
    assert len(h.orders("SL")) == 1  # verified first time: no re-place
    event = h.audit("trade.protected")[0]
    import json

    assert json.loads(event["payload"])["ms"] < h.settings.protect_soft_ms
    assert h.app.metrics.quantile("time_to_protected_ms", 0.95) < h.settings.protect_soft_ms
    assert any("Order Placed" in text for text in h.sent) and any("verified on exchange" in text for text in h.sent)


async def test_tc021_sl_acknowledged_but_absent_is_replaced_once_then_flattened_at_the_hard_deadline(h):
    h.sim.faults.hide_open_order = lambda order: order.is_trigger and order.tpsl == "sl"
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", timeout=6, message="flattened and closed")
    trade = h.trade()
    assert trade["outcome"] == "FLATTENED" and trade["close_reason"] == "protection_failure"
    sls = h.orders("SL")
    assert len(sls) == 2 and sls[0]["attempt"] == 1 and sls[1]["attempt"] == 2  # re-placed exactly once
    assert sls[0]["cloid"] != sls[1]["cloid"]
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()  # flat, no leftovers
    assert h.orders("CLOSE")[0]["reduce_only"] and h.orders("CLOSE")[0]["tif"] == "Ioc"
    # §10.7: the asset is paused until a manual resume
    assert not h.health.can_enter(COIN)[0] and h.health.has_condition(f"protection_failure:{COIN}")
    assert any("could not be protected" in text for text in h.alerts())
    h.health.resume()
    assert h.health.can_enter(COIN)[0]


async def test_tc022_sl_already_through_the_market_flattens_immediately(h):
    h.sim.faults.on_place = lambda order: "reject:Invalid TP/SL price." if is_sl(order) else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", message="closed")
    trade = h.trade()
    assert trade["outcome"] == "FLATTENED" and trade["close_reason"] == "sl_invalid"
    assert len(h.orders("SL")) == 1  # no retry: straight to flatten (§10.5 step 7)
    assert h.sim.positions[COIN][0] == 0


async def test_sl_rejected_twice_flattens_without_waiting_for_the_deadline(h):
    h.sim.faults.on_place = lambda order: "reject:No liquidity available for market order." if is_sl(order) else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", message="closed")
    assert h.trade()["outcome"] == "FLATTENED"
    assert [o["state"] for o in h.orders("SL")] == ["REJECTED", "REJECTED"]


async def test_tc023_sl_cancelled_out_of_band_is_replaced_and_verified_with_an_alert(h):
    trade = await h.open_protected_trade()
    original = h.orders("SL")[0]
    await h.sim.external_cancel(original["cloid"])
    await h.until(lambda: len(h.orders("SL")) == 2 and h.trade()["state"] == "PROTECTED", timeout=5, message="SL re-placed and verified")
    replacement = h.orders("SL")[1]
    assert h.orders("SL")[0]["state"] == "CANCELED"
    assert replacement["cloid"] in {o.cloid for o in h.sim.open_orders()} and replacement["trigger_price"] == 4296.0
    assert replacement["size"] == pytest.approx(trade["filled_size"])
    assert any("Stop-loss missing" in text for text in h.alerts())
    states = [e["payload"] for e in h.audit("trade.state")]
    assert any('"from": "PROTECTED", "to": "OPEN_UNPROTECTED"' in p for p in states)


async def test_fr046_a_missing_sl_is_replaced_even_while_paused(h):
    await h.open_protected_trade()
    await h.app.command("pause", {"reason": "test"})
    assert not h.health.can_enter(COIN)[0]
    await h.sim.external_cancel(h.orders("SL")[0]["cloid"])
    await h.until(lambda: len(h.orders("SL")) == 2 and h.trade()["state"] == "PROTECTED", timeout=5, message="SL repaired while paused")
    assert h.health.operator_paused()


async def test_tc025_flatten_failure_halts_and_the_alert_repeats_until_acknowledged(make):
    h = await make(flatten_deadline_ms=300)
    h.app._last_halt_alert = -1e9
    h.sim.faults.hide_open_order = lambda order: order.is_trigger and order.tpsl == "sl"
    h.sim.faults.on_place = lambda order: "reject:No liquidity available for market order." if order.reduce_only and order.tif == "Ioc" and order.kind == "limit" else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "HALTED_MANUAL", timeout=6, message="HALTED_MANUAL")
    assert h.health.state() == "HALTED" and h.health.has_condition("flatten_unverified")
    assert not h.health.can_enter(COIN)[0]
    assert any("HALTED" in text and "could not be flattened" in text for text in h.alerts())
    await h.until(lambda: any("STILL HALTED" in text for text in h.alerts()), message="repeating alert")
    repeats = sum("STILL HALTED" in text for text in h.alerts())
    await h.app.command("ack")
    h.app._last_halt_alert = -1e9
    await h.settle()
    assert sum("STILL HALTED" in text for text in h.alerts()) == repeats  # acknowledged: no more repeats
    assert h.health.state() == "HALTED"  # acknowledging is not resuming

    # The engine keeps trying; once the exchange accepts the close, the trade ends FLATTENED.
    h.sim.faults.on_place = None
    h.engine.halted_poll_s = 0.05
    h.engine.wake(h.trade()["id"])
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="closed after the exchange recovered")
    assert h.trade()["outcome"] == "FLATTENED" and h.sim.positions[COIN][0] == 0
    assert h.health.state() == "HALTED"  # still needs a manual resume
    await h.app.command("resume")
    assert h.health.state() == "HEALTHY"


async def test_tc030_partial_entry_fill_is_accepted_and_the_ladder_is_replanned_on_the_fill(h):
    h.sim.faults.on_place = lambda order: "partial:0.4" if not order.reduce_only else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    trade = h.trade()
    assert trade["filled_size"] == pytest.approx(round(trade["planned_size"] * 0.4 - 0.00005, 4), abs=1e-4)
    assert trade["filled_size"] < trade["planned_size"]
    entry = h.orders("ENTRY")[0]
    assert entry["state"] == "CANCELED" and entry["filled_size"] == pytest.approx(trade["filled_size"])  # IOC remainder cancelled
    assert h.orders("SL")[0]["size"] == pytest.approx(trade["filled_size"])
    assert sum(o["size"] for o in h.orders("TP")) == pytest.approx(trade["filled_size"])
    assert h.audit("trade.partial_fill")
    assert any("Partially Filled" in text for text in h.sent)


async def test_partial_fill_policy_flatten_closes_the_partial_fill(make):
    h = await make(partial_fill_policy="flatten")
    h.sim.faults.on_place = lambda order: "partial:0.4" if not order.reduce_only else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", message="closed")
    assert h.trade()["outcome"] == "FLATTENED" and h.trade()["close_reason"] == "partial_fill_policy"
    assert not h.orders("SL") and h.sim.positions[COIN][0] == 0


async def test_tc031_a_fill_too_small_for_one_minimum_tp_leg_is_always_flattened(h):
    h.sim.faults.min_notional_on_reduce_only = False  # IV-07 favourable case: the closing remainder is accepted
    h.sim.faults.on_place = lambda order: "partial:0.005" if not order.reduce_only else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", message="closed")
    trade = h.trade()
    assert trade["filled_size"] * 4287.0 < 10.0
    assert trade["outcome"] == "FLATTENED" and trade["close_reason"] == "too_small"
    assert h.sim.positions[COIN][0] == 0


async def test_tc032_partial_tp_fill_keeps_remaining_size_and_sl_coverage_correct(h):
    trade = await h.open_protected_trade()
    tp1 = h.orders("TP")[0]
    # Someone else closes half of TP1's size worth of the position at the TP1 price level:
    # simulate a partial TP fill by shrinking what the market offers.
    order = h.sim.orders[h.sim.by_cloid[tp1["cloid"]]]
    half = round(tp1["size"] / 2, 4)
    h.sim._fill(order, 4283.0, half, taker=False)
    await h.sim.drain()
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING after a partial TP fill")
    after = h.trade()
    assert after["remaining_size"] == pytest.approx(trade["filled_size"] - half)
    assert h.db.get("orders", tp1["id"])["state"] == "PARTIALLY_FILLED"
    await h.until(lambda: h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]].remaining == pytest.approx(after["remaining_size"]), message="SL resized")
    live_tp = sum(o.size for o in h.sim.open_orders() if not o.is_trigger)
    assert live_tp == pytest.approx(after["remaining_size"])


async def test_tc052_sl_request_times_out_but_was_accepted_it_is_resolved_by_cloid_not_replaced(h):
    h.sim.faults.on_place = lambda order: "drop" if is_sl(order) and len(h.orders("SL")) == 1 else None
    trade = await h.open_protected_trade()
    assert len(h.orders("SL")) == 1 and h.orders("SL")[0]["state"] == "RESTING"
    assert sum(1 for o in h.sim.open_orders() if o.is_trigger) == 1
    assert h.app.metrics.counter("unknown_outcomes") == 1
    assert trade["state"] == "PROTECTED"


async def test_tc052_sl_request_times_out_and_was_not_accepted_it_is_replaced(h):
    lost = []

    def on_place(order):
        if is_sl(order) and not lost:
            lost.append(order.cloid)
            return "lose"
        return None

    h.sim.faults.on_place = on_place
    await h.open_protected_trade()
    sls = h.orders("SL")
    assert [o["state"] for o in sls] == ["NOT_ACCEPTED", "RESTING"]
    assert sum(1 for o in h.sim.open_orders() if o.is_trigger) == 1


async def test_fr054_sl_coverage_is_reverified_after_each_tp_fill(h):
    trade = await h.open_protected_trade()
    await h.quote(4282.9, 4283.0)  # ask reaches TP1
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING")
    after = h.trade()
    tp1 = h.orders("TP")[0]
    assert h.db.get("orders", tp1["id"])["state"] == "FILLED"
    assert after["remaining_size"] == pytest.approx(trade["filled_size"] - tp1["size"])
    await h.until(
        lambda: h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]].remaining == pytest.approx(after["remaining_size"]),
        message="SL resized to the remaining position",
    )
    sl = h.orders("SL")[0]
    assert sl["state"] == "RESTING" and sl["size"] == pytest.approx(after["remaining_size"])
    assert len(h.orders("SL")) == 1  # modified in place, not re-placed
    remaining_tps = [o for o in h.sim.open_orders() if not o.is_trigger]
    assert sum(o.size for o in remaining_tps) == pytest.approx(after["remaining_size"])


async def test_fr064_breakeven_after_tp1_when_enabled(make):
    h = await make(sl_breakeven_after_tp1=True)
    trade = await h.open_protected_trade()
    await h.quote(4282.9, 4283.0)
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING")
    await h.until(lambda: h.orders("SL")[0]["trigger_price"] == trade["avg_entry"], message="SL moved to break-even")
    on_exchange = h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]]
    assert on_exchange.req.trigger_px == trade["avg_entry"] == 4287.0


async def test_fr064_is_off_by_default(h):
    await h.open_protected_trade()
    await h.quote(4282.9, 4283.0)
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING")
    await h.settle()
    assert h.orders("SL")[0]["trigger_price"] == 4296.0


async def test_tp_rejection_never_causes_a_flatten(h):
    h.sim.faults.on_place = lambda order: "reject:Order price too far from oracle" if order.reduce_only and order.kind == "limit" and order.tif == "Gtc" else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED without TPs")
    assert h.orders("SL")[0]["state"] == "RESTING"
    assert all(o["state"] == "REJECTED" for o in h.orders("TP"))
    assert h.audit("tp.rejected") and h.sim.positions[COIN][0] != 0


async def test_fr063_operator_sl_modify_is_applied_and_reverified(h):
    await h.open_protected_trade()
    trade_id = h.trade()["id"]
    result = await h.app.command("modify_sl", {"trade_id": trade_id, "price": 4294.0})
    assert result["ok"]
    await h.settle()
    assert h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]].req.trigger_px == 4294.0
    assert h.trade()["sl_price"] == 4294.0 and h.trade()["state"] == "PROTECTED"
    # A modify the exchange rejects leaves the previous stop in place and alerts.
    result = await h.app.command("modify_sl", {"trade_id": trade_id, "price": 4280.0})  # below the market for a short: invalid
    assert not result["ok"]
    assert h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]].req.trigger_px == 4294.0
    assert h.orders("SL")[0]["state"] == "RESTING" and any("modify rejected" in t for t in h.alerts())


def test_error_class_used_for_protection_is_the_documented_one():
    from hlbot.exchange.errors import classify_error

    assert classify_error("Invalid TP/SL price.") == ErrorClass.INVALID_TPSL
