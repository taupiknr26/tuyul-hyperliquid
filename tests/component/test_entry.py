"""Entry flow against the fake exchange (TC-001, TC-002, TC-004..TC-006,
TC-010, TC-011, TC-013, TC-036, TC-060..TC-064, TC-085, TC-086)."""

import pytest

from tests.component.conftest import BUY_TEXT, COIN, SELL_TEXT


async def test_tc001_valid_sell_is_entered_and_protected(h):
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")

    trade = h.trade()
    assert h.signal(signal_id)["state"] == "TRADED"
    assert trade["side"] == "SELL" and trade["filled_size"] == trade["planned_size"] > 0
    assert trade["avg_entry"] == 4287.0  # filled at the bid, never worse than the limit
    entry = h.orders("ENTRY")[0]
    assert entry["state"] == "FILLED" and entry["tif"] == "Ioc" and entry["price"] == 4284.0 and not entry["reduce_only"]
    assert entry["expires_after"] is not None

    open_orders = {o.cloid: o for o in h.sim.open_orders()}
    sl = h.orders("SL")[0]
    on_exchange = open_orders[sl["cloid"]]
    assert sl["state"] == "RESTING"
    assert on_exchange.is_trigger and on_exchange.reduce_only and on_exchange.tpsl == "sl" and on_exchange.is_buy
    assert on_exchange.trigger_px == 4296.0 and on_exchange.size == pytest.approx(trade["filled_size"])

    tps = h.orders("TP")
    assert [o["price"] for o in tps] == [4283.0, 4280.0, 4277.0, 4274.0, 4270.0]
    assert all(o["reduce_only"] and o["state"] == "RESTING" and o["side"] == "BUY" for o in tps)
    assert sum(o["size"] for o in tps) == pytest.approx(trade["filled_size"])  # FR-051
    assert h.sim.positions[COIN][0] == pytest.approx(-trade["filled_size"])
    assert len({o["cloid"] for o in h.orders()}) == len(h.orders())


async def test_tc002_valid_buy_mirror(h):
    await h.quote(4311.0, 4311.1)
    signal_id = h.post(BUY_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    trade = h.trade()
    assert h.signal(signal_id)["state"] == "TRADED" and trade["side"] == "BUY"
    assert trade["avg_entry"] == 4311.1  # the ask
    sl = {o.cloid: o for o in h.sim.open_orders()}[h.orders("SL")[0]["cloid"]]
    assert not sl.is_buy and sl.trigger_px == 4303.0
    assert [o["price"] for o in h.orders("TP")] == [4316.0, 4319.0, 4322.0, 4325.0, 4329.0]
    assert h.sim.positions[COIN][0] == pytest.approx(trade["filled_size"])


async def test_signal_waits_outside_the_range_then_enters_on_the_first_in_range_quote(h):
    await h.quote(4300.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.trades()
    await h.quote(4291.2)  # just outside the buffered upper edge 4291.0 (TC-006)
    assert not h.trades()
    await h.quote(4291.0)  # exactly on the edge
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    assert h.signal(signal_id)["state"] == "TRADED"


async def test_tc005_signal_never_in_range_expires_with_no_order_and_one_notice(h):
    await h.quote(4300.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    h.clock.advance(30 * 60, monotonic=False)  # exactly at expiry: not entered at or after
    await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["state"] == "EXPIRED", message="EXPIRED")
    assert not h.trades() and not h.sim.requests
    skipped = h.db.all("SELECT * FROM outbox WHERE kind = 'signal_skipped'")
    assert len(skipped) == 1 and "Expired" in skipped[0]["text"]


async def test_tc004_unmapped_symbol_and_delisted_coin_are_rejected_with_a_reason(make):
    h = await make()
    await h.quote(4287.0)
    unmapped = h.post(SELL_TEXT.replace("#GOLD", "#SILVER"))
    assert h.signal(unmapped)["state"] == "INVALID" and "unmapped symbol" in h.signal(unmapped)["reason"]
    assert not h.trades()

    import dataclasses

    h.sim.instruments[COIN] = dataclasses.replace(h.sim.instruments[COIN], is_delisted=True)
    await h.app.reconciler.refresh_instruments()
    delisted = h.post(SELL_TEXT)
    assert h.signal(delisted)["state"] == "INVALID" and "delisted" in h.signal(delisted)["reason"]
    assert h.health.has_condition(f"asset_blocked:{COIN}")
    assert not h.sim.requests


async def test_tc011_ioc_with_no_liquidity_inside_the_limit_returns_to_waiting_and_counts_the_attempt(make):
    h = await make(entry_attempts=2)
    await h.quote(4287.0)
    h.sim.faults.on_place = lambda order: "reject:Order could not immediately match against any resting orders." if not order.reduce_only else None
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.signal(signal_id)["state"] == "FAILED", message="FAILED after attempts")
    signal = h.signal(signal_id)
    assert signal["attempts"] == 2 and "attempts exhausted" in signal["reason"]
    assert [t["state"] for t in h.trades()] == ["ABORTED", "ABORTED"]
    assert [o["state"] for o in h.orders("ENTRY")] == ["REJECTED", "REJECTED"]
    assert len({o["cloid"] for o in h.orders("ENTRY")}) == 2  # each attempt has its own cloid
    assert h.sim.positions.get(COIN, [0, 0])[0] == 0


async def test_tc013_limit_is_clamped_against_tp1_and_waits_when_it_cannot_cross(h):
    await h.quote(4284.0)  # lower edge of the buffered range: 4284 - 3 would be beyond TP1 4283
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    assert h.orders("ENTRY")[0]["price"] == 4283.5

    h2_signal = None
    # A second, distinct signal whose TP1 sits inside the buffered range: the clamp cannot cross.
    text = SELL_TEXT.replace("4286- 4289", "4285- 4288").replace("TP1 : 4283", "TP1 : 4284.8")
    await h.app.command("close", {"trade_id": h.trade()["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="first trade closed")
    await h.quote(4285.0)
    h2_signal = h.post(text)
    await h.settle()
    assert h.signal(h2_signal)["state"] == "WAITING_PRICE"
    assert any("LIMIT_CLAMP" in e["payload"] for e in h.audit("signal.waiting"))


async def test_tc036_exchange_margin_rejection_is_a_risk_rejection_with_no_retry(h):
    await h.quote(4287.0)
    h.sim.faults.on_place = lambda order: "reject:Insufficient margin to place order." if not order.reduce_only else None
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.signal(signal_id)["state"] == "FAILED", message="FAILED")
    assert "exchange rejected" in h.signal(signal_id)["reason"]
    assert h.entry_requests() == 1 and h.signal(signal_id)["attempts"] == 1


async def test_tc060_same_message_delivered_twice_yields_one_trade(h):
    await h.quote(4287.0)
    first = h.post(SELL_TEXT, message_id=1477)
    assert h.post(SELL_TEXT, message_id=1477) is None  # duplicate delivery / backfill overlap
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    assert len(h.trades()) == 1 and h.signal(first)["state"] == "TRADED"
    assert h.db.scalar("SELECT COUNT(*) FROM messages") == 1


async def test_tc061_same_content_reposted_in_the_same_channel_is_a_duplicate(h):
    await h.open_protected_trade()
    await h.app.command("close", {"trade_id": h.trade()["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="closed")
    await h.quote(4287.0)
    again = h.post(SELL_TEXT)  # new message id, same zone, same UTC day
    await h.settle()
    await h.until(lambda: h.signal(again)["state"] == "DUPLICATE", message="DUPLICATE")
    assert h.signal(again)["reason"].startswith("DUPLICATE:")
    assert len(h.trades()) == 1


async def test_tc062_same_message_id_from_two_channels_are_both_stored_and_evaluated(h):
    """Fixes LIM-01."""
    await h.quote(4300.0)
    a = h.post(SELL_TEXT, channel=-1001, message_id=50, ref="@one")
    b = h.post(BUY_TEXT, channel=-1002, message_id=50, ref="@two")
    assert a is not None and b is not None and a != b
    assert h.db.scalar("SELECT COUNT(*) FROM messages WHERE message_id = 50") == 2
    assert h.signal(a)["state"] == "WAITING_PRICE" and h.signal(b)["state"] == "WAITING_PRICE"


async def test_tc063_racing_evaluations_of_one_signal_produce_one_exchange_order(h):
    import asyncio

    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await asyncio.gather(*(h.engine.try_enter(signal_id) for _ in range(8)))
    await h.settle()
    assert len(h.trades()) == 1 and h.entry_requests() == 1


async def test_tc064_same_content_from_a_second_channel_is_a_cross_channel_duplicate(h):
    await h.quote(4287.0)
    first = h.post(SELL_TEXT, channel=-1001, message_id=10, ref="@one")
    await h.settle()
    await h.until(lambda: h.signal(first)["state"] == "TRADED", message="TRADED")
    await h.app.command("close", {"trade_id": h.trade()["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="closed")
    await h.quote(4287.0)
    second = h.post(SELL_TEXT, channel=-1002, message_id=99, ref="@two")
    await h.settle()
    await h.until(lambda: h.signal(second)["state"] == "DUPLICATE", message="DUPLICATE")
    assert h.signal(second)["reason"].startswith("DUPLICATE_CROSS_CHANNEL")


async def test_fr035_new_signal_on_an_asset_with_an_open_trade_is_rejected_position_open(h):
    await h.open_protected_trade()
    other = h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h.quote(4287.0)
    await h.until(lambda: h.signal(other)["state"] == "REJECTED_RISK", message="REJECTED_RISK")
    assert "POSITION_OPEN" in h.signal(other)["reason"]
    assert h.entry_requests() == 1
    assessment = h.db.one("SELECT * FROM risk_assessments WHERE signal_id = ?", (other,))
    assert assessment["approved"] == 0 and "POSITION_OPEN" in assessment["violations"]


async def test_fr037_leverage_is_set_and_verified_on_the_exchange_before_the_first_order(h):
    assert h.sim.leverage.get(COIN) is None
    await h.open_protected_trade()
    assert h.sim.leverage[COIN] == h.settings.risk.leverage == 5
    kinds = [r["kind"] for r in h.sim.requests]
    assert kinds.index("leverage") < kinds.index("order")
    assert h.audit("leverage.verified")


async def test_tc086_asset_at_open_interest_cap_blocks_entries(h):
    h.sim.faults.oi_capped.add(COIN)
    await h.app.reconciler.refresh_instruments()
    assert not h.health.can_enter(COIN)[0]
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.sim.requests
    h.sim.faults.oi_capped.clear()
    await h.app.reconciler.refresh_instruments()
    await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["state"] == "TRADED", message="TRADED once the cap clears")


async def test_fr047_the_intent_row_is_committed_before_the_request_leaves(h):
    seen = {}

    def spy(order):
        row = h.db.one("SELECT state, cloid FROM orders WHERE cloid = ?", (order.cloid,))
        seen.setdefault(order.cloid, row["state"] if row else None)
        return None

    h.sim.faults.on_place = spy
    await h.open_protected_trade()
    assert seen and all(state == "SUBMITTING" for state in seen.values())  # persisted (INTENT -> SUBMITTING) before send
    intents = {e["order_id"] for e in h.audit("order.intent")}
    assert intents == {o["id"] for o in h.orders()}
    # For every order: its intent event precedes the exchange request that carried it.
    intent_seq = {e["order_id"]: e["seq"] for e in h.audit("order.intent")}
    for request in h.audit("exchange.request"):
        if '"action": "order"' in request["payload"]:
            assert intent_seq[request["order_id"]] < request["seq"]


async def test_fr024_stale_quote_blocks_the_entry_and_records_data_stale(h):
    await h.quote(4287.0)
    h.clock.advance(3.0, monotonic=False)  # the quote is now 3 s old
    h.sim.faults.rest_down = True  # and the REST refresh cannot re-confirm it
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.trades()
    assert any("DATA_STALE" in e["payload"] for e in h.audit("signal.waiting"))
    h.sim.faults.rest_down = False
