"""Several same-direction trades on one asset (FR-035 with
RISK_MAX_TRADES_PER_ASSET above 1). They share the single position the
exchange keeps for the asset; each keeps its own stop and targets."""

import pytest

from tests.component.conftest import COIN, SELL_TEXT

SECOND = SELL_TEXT.replace("4286- 4289", "4285- 4288").replace("SL : 4296", "SL : 4299")
THIRD = SELL_TEXT.replace("4286- 4289", "4284- 4287").replace("SL : 4296", "SL : 4302")
BUY_NEAR = "#GOLD BUY NOW\nGold buy Zone 4285- 4288\n\nSL : 4278\n\nTP1 : 4300\nTP2 : 4305\nTP3 : 4310\n#Vip_Signals"


async def stacked(make, count, **overrides):
    """A bot holding ``count`` protected SELL trades on the same asset."""
    h = await make(risk_max_trades_per_asset=3, risk_risk_per_trade_pct=0.1, risk_min_rr=0.2, **overrides)
    await h.open_protected_trade()
    for text in (SECOND, THIRD)[: count - 1]:
        want = len(h.trades()) + 1
        h.post(text)
        await h.quote(4287.0)
        await h.until(
            lambda want=want: len(h.trades()) == want and all(t["state"] == "PROTECTED" for t in h.trades()),
            timeout=6, message=f"{want} trades PROTECTED",
        )
    return h


def diverged(h):
    return h.health.has_condition(f"recon_divergence:{COIN}") or any(t["state"] == "UNRECONCILED" for t in h.trades())


def stop_on_exchange(h, trade_id):
    sl = h.orders("SL", trade_id)[-1]  # one stop per trade here; it may be mid-modify
    return h.sim.orders[h.sim.by_cloid[sl["cloid"]]]


async def test_three_same_direction_trades_share_one_position_each_with_its_own_stop_and_targets(make):
    h = await stacked(make, 3)
    trades = h.trades()
    assert [t["side"] for t in trades] == ["SELL"] * 3 and h.entry_requests() == 3
    assert h.sim.positions[COIN][0] == pytest.approx(-sum(t["filled_size"] for t in trades))
    open_orders = {o.cloid: o for o in h.sim.open_orders()}
    for trade, trigger in zip(trades, (4296.0, 4299.0, 4302.0), strict=True):
        sl = h.orders("SL", trade["id"])[0]
        assert sl["state"] == "RESTING" and open_orders[sl["cloid"]].trigger_px == trigger
        assert open_orders[sl["cloid"]].size == pytest.approx(trade["filled_size"])  # its own size, not the whole position
        tps = h.orders("TP", trade["id"])
        assert all(o["state"] == "RESTING" for o in tps) and sum(o["size"] for o in tps) == pytest.approx(trade["filled_size"])
    await h.settle(15)  # several reconciliation passes over the shared position
    assert all(t["state"] == "PROTECTED" for t in h.trades()) and not diverged(h)
    assert not [a for a in h.alerts() if "differs" in a or "did not place" in a or "Stop-loss" in a]

    # FR-035: the limit still holds.
    fourth = h.post(SELL_TEXT.replace("4286- 4289", "4287- 4290"))
    await h.quote(4287.0)
    await h.until(lambda: h.signal(fourth)["state"] == "REJECTED_RISK", message="REJECTED_RISK")
    assert "POSITION_OPEN" in h.signal(fourth)["reason"] and len(h.trades()) == 3 and h.entry_requests() == 3


async def test_a_signal_the_other_way_is_rejected_while_a_trade_is_open(make):
    h = await stacked(make, 1)
    position = h.sim.positions[COIN][0]
    other = h.post(BUY_NEAR)
    await h.quote(4287.0)
    await h.until(lambda: h.signal(other)["state"] == "REJECTED_RISK", message="REJECTED_RISK")
    assert "OPPOSITE_POSITION" in h.signal(other)["reason"]
    assert h.sim.positions[COIN][0] == position and h.entry_requests() == 1 and h.trade()["state"] == "PROTECTED"


async def test_one_trades_stop_fires_and_the_other_stays_protected(make):
    h = await stacked(make, 2)
    first, second = h.trades()
    await h.quote(4296.0, 4296.1)  # crosses the first stop (4296) only; the second is at 4299
    await h.until(lambda: h.trade(first["id"])["state"] == "CLOSED", timeout=6, message="first CLOSED")
    assert h.trade(first["id"])["outcome"] == "LOSS_ALL"
    await h.settle(15)
    second = h.trade(second["id"])
    assert second["state"] == "PROTECTED" and second["remaining_size"] == pytest.approx(second["filled_size"])
    assert h.sim.positions[COIN][0] == pytest.approx(-second["filled_size"])
    # What is left on the exchange is exactly the second trade's stop and targets.
    mine = {o["cloid"] for o in h.orders(trade_id=second["id"]) if o["role"] != "ENTRY"}
    assert {o.cloid for o in h.sim.open_orders()} == mine
    assert not diverged(h)


async def test_targets_fill_on_both_trades_and_each_stop_follows_its_own_remaining_size(make):
    h = await stacked(make, 2)
    await h.quote(4282.9, 4283.0)  # TP1 of both
    await h.until(lambda: all(t["state"] == "REDUCING" for t in h.trades()), timeout=6, message="both REDUCING")
    await h.settle(15)
    trades = h.trades()
    assert all(0 < t["remaining_size"] < t["filled_size"] for t in trades)
    assert h.sim.positions[COIN][0] == pytest.approx(-sum(t["remaining_size"] for t in trades))
    for trade in trades:
        assert stop_on_exchange(h, trade["id"]).remaining == pytest.approx(trade["remaining_size"])
    assert not diverged(h)


async def test_each_stacked_trade_is_accounted_from_its_own_entry(make):
    h = await make(risk_max_trades_per_asset=3, risk_risk_per_trade_pct=0.1, risk_min_rr=0.2)
    await h.open_protected_trade(bid=4287.0)
    h.post(SECOND)
    await h.quote(4288.5)
    await h.until(lambda: len(h.trades()) == 2 and all(t["state"] == "PROTECTED" for t in h.trades()), timeout=6, message="two PROTECTED")
    assert [t["avg_entry"] for t in h.trades()] == [4287.0, 4288.5]
    for trade in h.trades():
        await h.app.command("close", {"trade_id": trade["id"]})
    await h.until(lambda: all(t["state"] == "CLOSED" for t in h.trades()), timeout=6, message="both CLOSED")
    for trade in h.trades():
        closes = h.db.all("SELECT price, size FROM fills WHERE trade_id = ? AND role = 'CLOSE'", (trade["id"],))
        own = sum((trade["avg_entry"] - f["price"]) * f["size"] for f in closes)
        assert trade["realized_pnl"] == pytest.approx(own, abs=1e-6)
    fills = [f for f in h.sim.fills if f.coin == COIN]
    on_exchange = sum(f.closed_pnl for f in fills) - sum(f.fee for f in fills)
    assert sum(t["net_pnl"] for t in h.trades()) == pytest.approx(on_exchange, abs=0.01)  # the total is the exchange's
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()


async def test_a_foreign_add_to_a_shared_position_is_covered_by_the_oldest_stop_and_can_be_adopted(make):
    h = await stacked(make, 2)
    first, second = h.trades()
    extra = 0.02
    await h.sim.external_trade(COIN, False, extra)  # someone adds to the short outside the bot
    await h.until(lambda: h.trade(first["id"])["state"] == "UNRECONCILED", timeout=5, message="UNRECONCILED")
    assert not h.health.can_enter(COIN)[0]
    actual = first["filled_size"] + second["filled_size"] + extra
    await h.until(
        lambda: stop_on_exchange(h, first["id"]).remaining + stop_on_exchange(h, second["id"]).remaining == pytest.approx(actual),
        timeout=5, message="stops cover the actual position",
    )
    assert stop_on_exchange(h, second["id"]).remaining == pytest.approx(second["filled_size"])  # the newer stop is left alone
    assert h.trade(second["id"])["state"] == "PROTECTED"

    result = await h.app.command("reconcile", {"adopt": True, "trade_id": first["id"]})
    assert result["ok"]
    await h.until(lambda: h.trade(first["id"])["state"] == "PROTECTED", message="managed again")
    await h.settle(15)
    assert h.trade(first["id"])["remaining_size"] == pytest.approx(first["filled_size"] + extra)
    assert h.trade(second["id"])["remaining_size"] == pytest.approx(second["filled_size"])
    assert not diverged(h)


async def test_a_partial_manual_buy_back_never_shrinks_the_stops_and_the_trade_can_be_closed(make):
    h = await stacked(make, 2)
    first, second = h.trades()
    await h.sim.external_trade(COIN, True, 0.02)  # partial manual buy-back
    await h.until(lambda: h.trade(first["id"])["state"] == "UNRECONCILED", timeout=5, message="UNRECONCILED")
    await h.settle(15)
    assert stop_on_exchange(h, first["id"]).remaining == pytest.approx(first["filled_size"])
    assert stop_on_exchange(h, second["id"]).remaining == pytest.approx(second["filled_size"])
    result = await h.app.command("reconcile", {"close": True, "trade_id": first["id"]})
    assert result["ok"]
    await h.until(lambda: h.trade(first["id"])["state"] == "CLOSED", timeout=6, message="first CLOSED")
    await h.settle(15)
    second = h.trade(second["id"])
    assert second["state"] == "PROTECTED" and h.sim.positions[COIN][0] == pytest.approx(-second["filled_size"])
    assert not diverged(h)


async def test_kill_closes_every_stacked_trade(make):
    h = await stacked(make, 3)
    report = await h.app.command("kill")
    assert report["ok"] and report["closed"] == 3 and report["remaining"] == []
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()
    assert all(t["state"] == "CLOSED" and t["outcome"] == "FLATTENED" for t in h.trades())


async def test_stacked_trades_are_recovered_after_a_crash(make):
    h = await stacked(make, 2)
    await h.crash()
    h2 = await h.restart()
    await h2.settle(15)
    assert [t["state"] for t in h2.trades()] == ["PROTECTED", "PROTECTED"] and not diverged(h2)
    assert h2.app.reconciler.recovery_invariant_ok() and h2.engine.entries_enabled
    assert h2.sim.positions[COIN][0] == pytest.approx(-sum(t["filled_size"] for t in h2.trades()))
    assert h2.entry_requests() == 2
    await h2.stop()


async def test_with_the_default_of_one_trade_per_asset_a_second_signal_is_still_rejected(h):
    await h.open_protected_trade()
    other = h.post(SECOND)
    await h.quote(4287.0)
    await h.until(lambda: h.signal(other)["state"] == "REJECTED_RISK", message="REJECTED_RISK")
    assert "POSITION_OPEN" in h.signal(other)["reason"] and h.entry_requests() == 1
