"""Reconciliation, foreign state, kill switch, degraded connectivity and
rate limits (FR-043..FR-046, FR-058, FR-060; TC-053, TC-054, TC-080,
TC-081, TC-091, TC-092)."""

import pytest

from hlbot.exchange.base import StreamHandlers
from tests.component.conftest import COIN, SELL_TEXT


async def test_fr058_order_cancelled_out_of_band_is_reflected_locally(h):
    await h.open_protected_trade()
    tp3 = h.orders("TP")[2]
    h.sim._handlers = StreamHandlers()  # no stream: only the REST reconciliation can notice
    order = h.sim.orders[h.sim.by_cloid[tp3["cloid"]]]
    h.sim._cancel(order)
    await h.until(lambda: h.db.get("orders", tp3["id"])["state"] == "CANCELED", timeout=5, message="cancel reflected")
    # TP verification is non-critical: the leg is re-placed, the trade is never flattened.
    await h.until(lambda: len([o for o in h.orders("TP") if o["leg"] == 3]) == 2, timeout=5, message="TP3 re-placed")
    # The new row is written before the request leaves (intent first), so wait for the exchange to hold it too.
    await h.until(lambda: len([o for o in h.sim.open_orders() if not o.is_trigger]) == 5, timeout=5, message="TP3 resting on the exchange")
    assert h.trade()["state"] == "PROTECTED"


async def test_tc081_tp_fills_while_the_stream_is_down_and_is_discovered_by_rest_without_double_processing(h):
    opened = await h.open_protected_trade()
    live = h.sim._handlers
    h.sim._handlers = StreamHandlers()  # stream down
    await h.sim.set_quote(COIN, 4282.9, 4283.0)  # TP1 fills during the gap
    await h.until(lambda: h.trade()["state"] == "REDUCING", timeout=5, message="fill discovered by REST")
    tp1 = h.orders("TP")[0]
    assert h.db.get("orders", tp1["id"])["state"] == "FILLED"
    assert h.trade()["remaining_size"] == pytest.approx(opened["filled_size"] - tp1["size"])
    fills_before = h.db.scalar("SELECT COUNT(*) FROM fills")

    # Stream returns and replays its snapshot: every fill is delivered again.
    h.sim._handlers = live
    for fill in h.sim.fills:
        await h.engine.on_fill(fill, True)
    await h.settle()
    assert h.db.scalar("SELECT COUNT(*) FROM fills") == fills_before == 2  # keyed by tid: replays are harmless
    assert h.trade()["remaining_size"] == pytest.approx(opened["filled_size"] - tp1["size"])


async def test_tc080_entries_are_blocked_while_market_data_is_stale_and_resume_after_reconnect(h):
    await h.quote(4300.0)
    signal_id = h.post(SELL_TEXT)
    h.sim._health.connected = False  # the stream drops
    await h.until(lambda: h.health.has_condition("market_data_stale"), message="stale detected")
    assert not h.health.can_enter(COIN)[0]
    await h.sim.set_quote(COIN, 4287.0, 4287.1, emit=False)
    await h.engine.on_quote(await h.sim.get_book(COIN))  # even a fresh REST quote does not bypass a dead stream
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.trades()

    passes = h.app.metrics.counter("reconcile_passes")
    h.sim._health.connected = True
    await h.app._on_connection(False)
    await h.app._on_connection(True)  # reconnect triggers a full REST reconciliation
    await h.until(lambda: h.app.metrics.counter("reconcile_passes") > passes, message="reconcile after reconnect")
    await h.quote(4287.0)
    await h.until(lambda: not h.health.has_condition("market_data_stale"), message="fresh again")
    await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["state"] == "TRADED", timeout=5, message="entered after reconnect")
    assert h.app.metrics.counter("ws_reconnects") == 1


async def test_fr060_a_foreign_position_blocks_entries_raises_an_alert_and_is_never_modified(h):
    await h.quote(4287.0)
    await h.sim.external_trade(COIN, True, 0.05)  # the operator opens a manual long
    await h.app.reconciler.run_once("test")
    assert h.health.has_condition(f"foreign_state:{COIN}") and not h.health.can_enter(COIN)[0]
    assert any("not created by the bot" in t for t in h.alerts())
    signal_id = h.post(SELL_TEXT)
    await h.quote(4287.0)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.trades()
    assert h.sim.positions[COIN][0] == pytest.approx(0.05)  # untouched
    assert not [r for r in h.sim.requests if r["kind"] in ("order", "cancel")]
    # When the operator closes it, the block clears by itself.
    await h.sim.external_trade(COIN, False, 0.05)
    await h.app.reconciler.run_once("test")
    assert not h.health.has_condition(f"foreign_state:{COIN}")
    await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["state"] == "TRADED", timeout=5, message="entered once the foreign position is gone")


async def test_fr060_a_foreign_order_blocks_entries_and_is_left_alone(h):
    from hlbot.exchange.base import OrderRequest

    await h.quote(4287.0)
    await h.sim.place([OrderRequest(COIN, True, 0.01, 4200.0, False, "0x" + "f" * 32, "limit", "Gtc")], h.app.queue.next_nonce())
    await h.app.reconciler.run_once("test")
    assert h.health.has_condition(f"foreign_state:{COIN}")
    assert [o.cloid for o in h.sim.open_orders()] == ["0x" + "f" * 32]  # never cancelled by the bot


async def test_position_changed_by_a_foreign_order_marks_unreconciled_keeps_sl_on_the_actual_size(h):
    opened = await h.open_protected_trade()
    extra = 0.02
    await h.sim.external_trade(COIN, False, extra)  # someone adds to the short outside the bot
    await h.until(lambda: h.trade()["state"] == "UNRECONCILED", timeout=5, message="UNRECONCILED")
    assert h.health.has_condition(f"recon_divergence:{COIN}") and not h.health.can_enter(COIN)[0]
    assert any("did not place" in t for t in h.alerts())
    actual = opened["filled_size"] + extra
    await h.until(
        lambda: h.sim.orders[h.sim.by_cloid[h.orders("SL")[0]["cloid"]]].remaining == pytest.approx(actual), timeout=5,
        message="SL resized to the actual position",
    )
    # --adopt: the operator accepts the exchange position as the trade's size.
    result = await h.app.command("reconcile", {"adopt": True, "trade_id": opened["id"]})
    assert result["ok"]
    await h.until(lambda: h.trade()["state"] in ("PROTECTED", "REDUCING"), message="managed again")
    assert h.trade()["remaining_size"] == pytest.approx(actual)
    assert not h.health.has_condition(f"recon_divergence:{COIN}")


async def test_unreconciled_trade_can_be_closed_by_the_operator(h):
    opened = await h.open_protected_trade()
    await h.sim.external_trade(COIN, True, 0.05)  # partial manual buy-back
    await h.until(lambda: h.trade()["state"] == "UNRECONCILED", timeout=5, message="UNRECONCILED")
    result = await h.app.command("reconcile", {"close": True, "trade_id": opened["id"]})
    assert result["ok"]
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=5, message="CLOSED")
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()
    assert h.trade()["outcome"] == "MANUAL"


async def test_tc091_kill_with_an_open_trade_is_flat_with_no_bot_orders_and_stays_paused(h):
    await h.open_protected_trade()
    report = await h.app.command("kill")
    assert report["ok"] and report["closed"] == 1 and report["remaining"] == []
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()
    trade = h.trade()
    assert trade["state"] == "CLOSED" and trade["outcome"] == "FLATTENED" and trade["close_reason"] == "kill"
    assert h.health.operator_paused() and h.health.state() == "PAUSED"
    # Stays paused: a fresh signal is not entered until resume.
    signal_id = h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h.quote(4287.0)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and len(h.trades()) == 1
    assert any("Kill switch: flat and paused" in t for t in h.alerts())


async def test_tc092_kill_never_touches_a_foreign_position_or_order(h):
    from hlbot.exchange.base import OrderRequest

    opened = await h.open_protected_trade()
    # A foreign resting order and (after the bot's short) a foreign add to the position.
    await h.sim.place([OrderRequest(COIN, True, 0.01, 4200.0, False, "0x" + "e" * 32, "limit", "Gtc")], h.app.queue.next_nonce())
    await h.sim.external_trade(COIN, False, 0.03)
    await h.until(lambda: h.trade()["state"] == "UNRECONCILED", timeout=5, message="UNRECONCILED")
    await h.app.command("reconcile", {"adopt": False})  # plain pass
    h.db.kv_set(f"foreign_ack:{opened['id']}", 0)
    report = await h.app.command("kill")
    assert report["ok"]
    assert "0x" + "e" * 32 in {o.cloid for o in h.sim.open_orders()}  # the foreign order survives
    assert all(o.cloid == "0x" + "e" * 32 for o in h.sim.open_orders())  # every bot order is gone


async def test_kill_reports_what_remains_when_the_exchange_refuses_the_close(make):
    h = await make()
    await h.open_protected_trade()
    h.sim.faults.on_place = lambda order: "reject:No liquidity available for market order." if order.tif == "Ioc" else None
    report = await h.engine.kill(timeout_s=0.5)
    assert report["closed"] == 0 and report["remaining"][0]["coin"] == COIN
    assert any("KILL incomplete" in t for t in h.alerts())
    assert h.health.operator_paused()
    assert h.orders("SL")[0]["cloid"] in {o.cloid for o in h.sim.open_orders()}  # still protected while it retries


async def test_tc053_rate_limited_protective_action_is_retried_and_entries_are_blocked_meanwhile(h):
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    sl = h.orders("SL")[0]
    h.sim.faults.rate_limited_actions = 2  # the next two signed actions get HTTP 429
    await h.sim.external_cancel(sl["cloid"])
    await h.until(lambda: h.trade()["state"] == "PROTECTED" and len(h.orders("SL")) == 2, timeout=6, message="SL repaired despite 429s")
    assert h.app.metrics.counter("rate_limit_hits") == 2
    assert h.health.has_condition("rate_limit") or h.app.queue.throttled
    # Entries are refused while throttled; protective work was not.
    from hlbot.core.action_queue import Priority
    from hlbot.exchange.base import ErrorClass, ExchangeError

    async def noop(nonce):
        return "sent"

    with pytest.raises(ExchangeError) as info:
        await h.app.queue.submit(Priority.ENTRY, "order", noop)
    assert info.value.kind == ErrorClass.RATE_LIMIT
    assert await h.app.queue.submit(Priority.PROTECT, "order", noop) == "sent"


async def test_tc054_while_throttled_the_single_action_slot_goes_to_protection_first(h):
    import asyncio

    from hlbot.core.action_queue import Priority

    queue = h.app.queue
    ran = []
    gate = asyncio.Event()

    async def blocker(nonce):
        await gate.wait()
        return "blocker"

    def job(name):
        async def run(nonce):
            ran.append(name)
            return name

        return run

    first = asyncio.ensure_future(queue.submit(Priority.CANCEL, "x", blocker))
    await asyncio.sleep(0.02)
    waiting = [
        asyncio.ensure_future(queue.submit(Priority.CANCEL, "cancel", job("cancel"))),
        asyncio.ensure_future(queue.submit(Priority.CLOSE, "close", job("close"))),
        asyncio.ensure_future(queue.submit(Priority.ENTRY, "entry", job("entry"))),
        asyncio.ensure_future(queue.submit(Priority.PROTECT, "protect", job("protect"))),
    ]
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.gather(first, *waiting)
    assert ran == ["protect", "close", "cancel", "entry"]  # place/repair SL -> close -> cancel -> entry


async def test_rest_outage_blocks_entries_then_pauses_and_recovers_after_a_clean_pass(h):
    await h.open_protected_trade()
    h.sim.faults.rest_down = True
    await h.until(lambda: h.health.has_condition("rest_errors"), timeout=5, message="REST failures noticed")
    assert not h.health.can_enter(COIN)[0]
    assert h.trade()["state"] == "PROTECTED"  # exchange-resident orders keep protecting the position
    h.app.reconciler._unreachable_since = h.clock.monotonic() - 31  # 30 s of failed polling
    await h.app.reconciler.run_once("test")
    assert h.health.has_condition("exchange_unreachable") and h.health.state() == "PAUSED"
    h.sim.faults.rest_down = False
    await h.app.reconciler.run_once("test")
    assert not h.health.has_condition("exchange_unreachable") and not h.health.has_condition("rest_errors")
    assert h.health.state() == "HEALTHY"


async def test_clock_drift_above_two_seconds_pauses_entries_and_clears_automatically(h):
    import dataclasses

    from hlbot.core.engine import ExchangeView

    view = await h.engine.view()
    drifted = ExchangeView(dataclasses.replace(view.account, exchange_ms=view.account.received_ms - 2600), view.open_orders)
    for _ in range(4):  # the whole one-minute window shows the offset: it is drift, not one slow response
        h.app.reconciler._clock_drift(drifted)
    assert h.health.has_condition("clock_drift") and h.health.state() == "PAUSED"
    h.app.reconciler._clock_drift(view)  # a single in-sync sample is enough to clear it
    assert not h.health.has_condition("clock_drift")


async def test_orphaned_bot_order_left_on_the_exchange_after_its_trade_closed_is_cancelled(h):
    from hlbot.exchange.base import OrderRequest

    opened = await h.open_protected_trade()
    stray = h.orders("TP")[4]
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    # A request that "landed late": the exchange shows one of our cloids resting although the trade is closed.
    await h.sim.external_trade(COIN, False, 0.01)
    del h.sim.by_cloid[stray["cloid"]]
    await h.sim.place([OrderRequest(COIN, True, 0.01, 4270.0, True, stray["cloid"], "limit", "Gtc")], h.app.queue.next_nonce())
    assert stray["cloid"] in {o.cloid for o in h.sim.open_orders()}
    await h.app.reconciler.run_once("test")
    assert stray["cloid"] not in {o.cloid for o in h.sim.open_orders()}
    assert h.audit("order.orphan_cancel")


async def test_margin_usage_above_the_cap_blocks_entries_until_it_clears(make):
    h = await make(risk_margin_usage_cap=0.5)
    await h.quote(4287.0)
    h.sim.leverage[COIN] = 1
    await h.sim.external_trade(COIN, True, 0.15)  # about 643 USDC of margin on a 1,000 account at 1x
    await h.app.reconciler.run_once("test")
    assert h.health.has_condition("margin_usage")
    await h.sim.external_trade(COIN, False, 0.15)
    await h.app.reconciler.run_once("test")
    assert not h.health.has_condition("margin_usage")


async def test_position_and_account_snapshots_are_recorded_with_their_source(h):
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")
    snapshot = h.db.one("SELECT * FROM position_snapshots WHERE szi != 0 ORDER BY id DESC LIMIT 1")
    assert snapshot["coin"] == COIN and snapshot["szi"] < 0 and snapshot["source"] == "rest" and snapshot["liquidation_px"] > 4287.0
    assert h.db.scalar("SELECT COUNT(*) FROM account_snapshots") >= 1


async def test_liquidation_proximity_alerts_once_per_crossing(make):
    h = await make(liq_alert_fraction=0.25)
    await h.open_protected_trade()
    view = await h.engine.view()
    liq = view.account.position(COIN).liquidation_px
    entry = view.account.position(COIN).entry_px
    near = round(liq - 0.2 * (liq - entry), 1)
    trade = h.trade()
    import dataclasses

    h.engine.contexts[COIN] = dataclasses.replace(h.engine.contexts[COIN], mark=near)
    h.engine._check_liquidation_proximity(trade, view)
    h.engine._check_liquidation_proximity(trade, view)
    assert sum("Liquidation proximity" in t for t in h.alerts()) == 1
    h.engine.contexts[COIN] = dataclasses.replace(h.engine.contexts[COIN], mark=entry)
    h.engine._check_liquidation_proximity(trade, view)
    h.engine.contexts[COIN] = dataclasses.replace(h.engine.contexts[COIN], mark=near)
    h.engine._check_liquidation_proximity(trade, view)
    assert sum("Liquidation proximity" in t for t in h.alerts()) == 2
