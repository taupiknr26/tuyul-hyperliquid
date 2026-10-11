"""Trade closure, outcome classification and P/L accounting (FR-055,
FR-056, FR-075; TC-024, TC-040..TC-044)."""

import json

import pytest

from tests.component.conftest import COIN, SELL_TEXT


async def run_to_close(h, quotes):
    for bid, ask in quotes:
        await h.quote(bid, ask)
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    return h.trade()


def exchange_pnl(h, trade):
    fills = [f for f in h.sim.fills if f.coin == COIN]
    return sum(f.closed_pnl for f in fills) - sum(f.fee for f in fills)


async def test_tc040_all_tps_fill_is_win_all_and_pnl_matches_the_exchange_to_the_cent(h):
    opened = await h.open_protected_trade()
    equity_before = 1000.0
    trade = await run_to_close(h, [(4282.9, 4283.0), (4279.9, 4280.0), (4276.9, 4277.0), (4273.9, 4274.0), (4269.9, 4270.0)])
    assert trade["outcome"] == "WIN_ALL" and trade["remaining_size"] == 0
    assert all(o["state"] == "FILLED" for o in h.orders("TP"))
    # FR-075: P/L = sum of closed P/L - fees + funding
    assert trade["net_pnl"] == pytest.approx(exchange_pnl(h, trade), abs=0.005)
    assert trade["net_pnl"] == pytest.approx(trade["realized_pnl"] - trade["fees"] + trade["funding"], abs=1e-6)
    assert h.sim.equity() == pytest.approx(equity_before + trade["net_pnl"], abs=0.005)
    assert trade["net_pnl"] > 0 and trade["fees"] > 0
    # FR-055: nothing of ours is left on the exchange
    assert not h.sim.open_orders() and h.sim.positions[COIN][0] == 0
    sl = h.orders("SL")[0]
    assert sl["state"] == "CANCELED"
    assert opened["filled_size"] == pytest.approx(sum(f.size for f in h.sim.fills if not f.is_buy))
    closed = [t for t in h.sent if "ALL TARGETS HIT" in t]
    assert len(closed) == 1 and "Net PnL" in closed[0] and "[PAPER]" in closed[0]


async def test_tc041_two_tps_then_sl_is_win_partial(h):
    await h.open_protected_trade()
    await h.quote(4282.9, 4283.0)
    await h.quote(4279.9, 4280.0)
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING")
    trade = await run_to_close(h, [(4296.0, 4296.1)])  # mark crosses the stop
    assert trade["outcome"] == "WIN_PARTIAL"
    roles = [r["role"] for r in h.db.all("SELECT role FROM fills WHERE trade_id = ? AND role != 'ENTRY' ORDER BY time, tid", (trade["id"],))]
    assert roles == ["TP", "TP", "SL"]
    assert not h.sim.open_orders() and h.sim.positions[COIN][0] == 0
    assert all(o["state"] in ("FILLED", "CANCELED") for o in h.orders("TP"))


async def test_tc042_tc024_sl_only_is_loss_all_and_leftover_tps_are_cancelled(h):
    await h.open_protected_trade()
    trade = await run_to_close(h, [(4296.0, 4296.1)])
    assert trade["outcome"] == "LOSS_ALL" and trade["net_pnl"] < 0
    assert h.orders("SL")[0]["state"] == "FILLED"
    assert all(o["state"] == "CANCELED" for o in h.orders("TP"))
    assert not h.sim.open_orders()
    assert trade["net_pnl"] == pytest.approx(exchange_pnl(h, trade), abs=0.005)
    assert any("STOPPED OUT" in t for t in h.sent)
    # The loss stays inside the risk budget used for sizing (0.5 % of 1,000 = 5 USDC).
    assert abs(trade["net_pnl"]) <= 5.0 + 0.01


async def test_sl_slippage_beyond_the_allowance_raises_an_alert(h):
    await h.open_protected_trade()
    await h.quote(4299.0, 4299.5, mark=4299.0)  # gaps through the stop: fills 3.5 points past the trigger
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    assert h.trade()["outcome"] == "LOSS_ALL"
    assert any("slippage exceeded allowance" in t for t in h.alerts())


async def test_tc043_operator_closes_on_the_exchange_is_manual_and_orders_are_cleaned_up(h):
    opened = await h.open_protected_trade()
    await h.sim.external_trade(COIN, True, opened["filled_size"])  # buy back the whole short outside the bot
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    trade = h.trade()
    assert trade["outcome"] == "MANUAL"  # fixes LIM-11: not classified as a loss
    assert not h.sim.open_orders()
    assert all(o["state"] == "CANCELED" for o in h.orders("TP")) and h.orders("SL")[0]["state"] == "CANCELED"
    assert any("CLOSED MANUALLY" in t for t in h.sent)


async def test_tc044_liquidation_closes_as_liquidated_and_halts(h):
    await h.open_protected_trade()
    await h.sim.liquidate(COIN)
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.trade()["outcome"] == "LIQUIDATED"
    assert h.health.state() == "HALTED" and h.health.has_condition("liquidation")
    assert not h.health.can_enter(COIN)[0]
    assert any("LIQUIDATED" in t for t in h.sent)
    await h.app.command("resume")  # manual resume only
    assert h.health.state() == "HEALTHY"


async def test_fr062_manual_close_command_closes_reduce_only_and_cleans_up(h):
    opened = await h.open_protected_trade()
    result = await h.app.command("close", {"trade_id": opened["id"]})
    assert result["ok"]
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    trade = h.trade()
    assert trade["outcome"] == "MANUAL" and trade["close_reason"] == "manual"
    close = h.orders("CLOSE")[0]
    assert close["reduce_only"] and close["tif"] == "Ioc" and close["side"] == "BUY" and close["state"] == "FILLED"
    assert close["price"] > 4287.0  # bounded: mark + flatten slippage, never an unbounded market order
    assert close["price"] == pytest.approx(4287.05 * 1.01, abs=0.11)
    assert not h.sim.open_orders() and h.sim.positions[COIN][0] == 0


async def test_fr090_any_fill_traces_to_order_trade_assessment_signal_and_message_in_one_query(h):
    await h.open_protected_trade()
    await h.quote(4282.9, 4283.0)
    await h.until(lambda: h.trade()["state"] == "REDUCING", message="REDUCING")
    rows = h.db.all("SELECT * FROM fill_trace ORDER BY fill_tid")
    assert len(rows) == 2
    for row in rows:
        assert row["order_cloid"] and row["trade_id"] and row["risk_assessment_id"] and row["signal_id"]
        assert row["message_text"] == SELL_TEXT and row["channel_id"] == -1004307627275 and row["message_id"] == 1477
        assert row["signal_content_id"] == "GOLD-SELL-4286-4289-20260914"
    assert [r["order_role"] for r in rows] == ["ENTRY", "TP"]
    assert json.loads(rows[0]["risk_outputs"])["size"] == h.trade()["planned_size"]

    from hlbot.api import readmodel

    trace = readmodel.trace(h.db, fill=rows[1]["fill_tid"])
    kinds = [e["kind"] for e in trace["audit"]]
    for expected in ("message.received", "signal.state", "order.intent", "exchange.request", "exchange.response", "fill", "trade.protected"):
        assert expected in kinds
    # One correlation id ties the whole chain together, across tasks.
    assert {e["correlation_id"] for e in trace["audit"]} == {f"sig-{rows[0]['signal_id']}"}


async def test_nfr009_every_exchange_request_has_a_recorded_response(h):
    await h.open_protected_trade()
    await h.app.command("close", {"trade_id": h.trade()["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    requests = [json.loads(e["payload"])["nonce"] for e in h.audit("exchange.request")]
    responses = [json.loads(e["payload"])["nonce"] for e in h.audit("exchange.response")]
    assert requests and requests == responses
    assert requests == sorted(requests) and len(set(requests)) == len(requests)  # one writer, strictly increasing nonces
    assert len(requests) == len(h.sim.requests)
