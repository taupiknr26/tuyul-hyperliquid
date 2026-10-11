"""Less-travelled paths of the engine and reconciler: exchange error
classes on entry (TC-085), integration-verification fallbacks (IV-14,
trigger-style TPs), startup failures, limits, signer expiry."""

import dataclasses

import pytest

from hlbot.core.reconciler import StartupError
from hlbot.exchange.base import ErrorClass, ExchangeError, OutcomeUnknown, StreamHandlers
from tests.component.conftest import COIN, SELL_TEXT, Harness


def entry(order):
    return not order.reduce_only


def is_sl(order):
    return order.kind == "trigger" and order.tpsl == "sl"


# ================================================================== TC-085: exchange rejections on entry
async def reject_entry(h, text, state="FAILED"):
    h.sim.faults.on_place = lambda order: f"reject:{text}" if entry(order) else None
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.signal(signal_id)["state"] == state, message=state)
    return signal_id


async def test_auth_rejection_pauses_until_a_manual_resume_with_a_critical_alert(h):
    signal_id = await reject_entry(h, "User or API Wallet 0xabc does not exist.", state="WAITING_PRICE")
    assert h.health.has_condition("agent_key") and h.health.state() == "PAUSED"
    assert any("PAUSED" in a and "rejected the signer" in a for a in h.alerts())
    await h.quote(4287.0)
    assert h.entry_requests() == 1  # nothing more is sent while paused
    assert h.signal(signal_id)["attempts"] == 1


async def test_tick_or_minimum_value_rejection_is_reported_as_a_defect_and_never_retried(h):
    signal_id = await reject_entry(h, "Price must be divisible by tick size.")
    assert "failed exchange validation" in h.signal(signal_id)["reason"]
    assert any("DEFECT" in a for a in h.alerts()) and h.entry_requests() == 1


async def test_unrecognised_rejection_fails_the_signal_without_retry(h):
    signal_id = await reject_entry(h, "Something the documentation never mentioned")
    assert "exchange rejected the entry" in h.signal(signal_id)["reason"] and h.entry_requests() == 1


async def test_margin_tier_rejection_is_a_risk_rejection(h):
    signal_id = await reject_entry(h, "Order would cause position to exceed margin tier limit at current leverage")
    assert h.signal(signal_id)["state"] == "FAILED" and any("rejected by exchange" in a for a in h.alerts())


async def test_open_interest_cap_rejection_blocks_the_asset_and_the_signal_keeps_waiting(h):
    signal_id = await reject_entry(h, "Order would exceed open interest cap.", state="WAITING_PRICE")
    assert h.health.has_condition(f"asset_blocked:{COIN}") and not h.health.can_enter(COIN)[0]
    assert h.trades()[0]["state"] == "ABORTED"
    assert h.signal(signal_id)["attempts"] == 1


async def test_price_too_far_from_oracle_waits_and_retries_within_the_attempt_budget(make):
    h = await make(entry_attempts=3)
    calls = []

    def on_place(order):
        if entry(order):
            calls.append(order.cloid)
            return "reject:Order price too far from oracle" if len(calls) < 3 else None
        return None

    h.sim.faults.on_place = on_place
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    for _ in range(4):
        await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["state"] == "TRADED", timeout=6, message="third attempt fills")
    assert len(set(calls)) == 3 and h.signal(signal_id)["attempts"] == 3
    assert [t["state"] for t in h.trades()][:2] == ["ABORTED", "ABORTED"]


async def test_entry_retry_spacing_is_respected(make):
    h = await make(entry_retry_min_s=2.0, entry_attempts=5)
    h.sim.faults.on_place = lambda order: "reject:Order could not immediately match against any resting orders." if entry(order) else None
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    for _ in range(5):
        await h.quote(4287.0)
    assert h.signal(signal_id)["attempts"] == 1  # further quotes inside 2 s do not trigger another attempt
    h.clock.advance(2.1, monotonic=False)
    await h.quote(4287.0)
    await h.until(lambda: h.signal(signal_id)["attempts"] == 2, message="second attempt after the spacing")


# ================================================================== pre-trade guards
async def test_no_quote_at_all_waits_with_data_stale(h):
    h.engine.quotes.clear()
    h.sim.faults.rest_down = True
    signal_id = h.post(SELL_TEXT)
    await h.engine.try_enter(signal_id)
    assert h.signal(signal_id)["state"] == "WAITING_PRICE"
    assert any("DATA_STALE: no quote" in e["payload"] for e in h.audit("signal.waiting"))
    h.sim.faults.rest_down = False


async def test_unreadable_account_fails_closed_and_waits(h):
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    real = h.sim.get_account

    async def broken():
        raise OutcomeUnknown("timeout")

    h.sim.get_account = broken
    h.freeze_background()
    await h.engine.try_enter(signal_id)
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.trades()
    assert any("ACCOUNT_UNVERIFIABLE" in e["payload"] for e in h.audit("signal.waiting"))
    h.sim.get_account = real


async def test_stale_market_context_is_refreshed_over_rest_and_basis_guard_uses_it(make):
    h = await make(risk_max_mark_oracle_dev_points=3.0)
    await h.quote(4287.0, mark=4287.0, oracle=4283.0)  # mark and oracle 4 points apart
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.signal(signal_id)["state"] == "REJECTED_RISK", message="basis rejection")
    assert "BASIS_EXCEEDED" in h.signal(signal_id)["reason"]

    h.engine.contexts.clear()  # no stream context: the engine reads it over REST
    other = h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h.quote(4287.0, mark=4287.0, oracle=4286.5)
    await h.until(lambda: h.signal(other)["state"] == "TRADED", message="traded once the basis is inside the guard")


async def test_foreign_position_discovered_at_entry_time_blocks_that_entry(h):
    await h.quote(4287.0)
    await h.sim.external_trade(COIN, True, 0.05)
    h.freeze_background()
    signal_id = h.post(SELL_TEXT)
    await h.engine.try_enter(signal_id)
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and h.health.has_condition(f"foreign_state:{COIN}")
    assert h.entry_requests() == 0 and h.sim.positions[COIN][0] == pytest.approx(0.05)


async def test_leverage_that_cannot_be_verified_blocks_the_entry(h):
    await h.quote(4287.0)

    async def refuse(coin, leverage, cross, nonce):
        raise ExchangeError(ErrorClass.OTHER, "Cannot change leverage")

    h.sim.set_leverage = refuse
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and h.entry_requests() == 0
    assert any("LEVERAGE_UNVERIFIED" in e["payload"] for e in h.audit("signal.waiting"))


async def test_market_moving_out_of_range_during_pre_trade_checks_cancels_the_decision(h):
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    real = h.sim.get_account

    async def slow_account():
        result = await real()
        await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
        await h.engine.on_quote(h.sim.quotes[COIN])  # the price leaves the range while the account is being read
        return result

    h.sim.get_account = slow_account
    h.freeze_background()
    await h.engine.try_enter(signal_id)
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and h.entry_requests() == 0
    assert any("MOVED" in e["payload"] for e in h.audit("signal.waiting"))


async def test_fixed_size_mode_trades_the_configured_size(make):
    h = await make(risk_sizing_mode="fixed", risk_fixed_size=0.05)
    trade = await h.open_protected_trade()
    assert trade["planned_size"] == 0.05 and trade["filled_size"] == 0.05


async def test_duplicate_protection_can_be_disabled_off_mainnet(make):
    h = await make(duplicate_protection=False)
    await h.open_protected_trade()
    await h.app.command("close", {"trade_id": h.trade()["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="closed")
    await h.quote(4287.0)
    again = h.post(SELL_TEXT)
    await h.until(lambda: h.signal(again)["state"] == "TRADED", message="traded again")


# ================================================================== protection edge cases
async def test_whole_request_failure_while_placing_the_sl_is_retried_once_then_flattened(h):
    real = h.sim.place

    async def place(orders, nonce, expires_after=None):
        if any(is_sl(o) for o in orders):
            raise ExchangeError(ErrorClass.OTHER, "internal error")
        return await real(orders, nonce, expires_after)

    h.sim.place = place
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", timeout=6, message="flattened")
    assert [o["state"] for o in h.orders("SL")] == ["REJECTED", "REJECTED"] and h.trade()["outcome"] == "FLATTENED"


async def test_an_unexpected_exception_while_sending_is_treated_as_unknown_never_as_failure(h):
    real = h.sim.place
    blown = []

    async def place(orders, nonce, expires_after=None):
        result = await real(orders, nonce, expires_after)
        if any(is_sl(o) for o in orders) and not blown:
            blown.append(1)
            raise ValueError("bug in the transport layer")  # after the exchange accepted the stop
        return result

    h.sim.place = place
    trade = await h.open_protected_trade()
    assert len(h.orders("SL")) == 1 and h.orders("SL")[0]["state"] == "RESTING"  # found by cloid, not re-sent
    assert trade["state"] == "PROTECTED"


async def test_trigger_style_take_profits_fill_on_mark_and_win_all(make):
    """OQ-06 alternative / IV-04 fallback."""
    h = await make(tp_style="trigger")
    await h.open_protected_trade()
    tps = h.orders("TP")
    assert all(o["type"] == "trigger" and o["trigger_price"] for o in tps)
    on_exchange = [o for o in h.sim.open_orders() if o.is_trigger and o.tpsl == "tp"]
    assert len(on_exchange) == 5 and all(o.reduce_only for o in on_exchange)
    for bid in (4282.9, 4279.9, 4276.9, 4273.9, 4269.9):
        await h.quote(bid, round(bid + 0.1, 1), mark=bid)
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.trade()["outcome"] == "WIN_ALL"


async def test_iv14_stop_fills_carrying_an_unknown_order_id_are_attributed_to_the_stop(h):
    await h.open_protected_trade()
    h.sim.faults.anonymous_trigger_fills = True
    await h.quote(4296.0, 4296.1)
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.trade()["outcome"] == "LOSS_ALL"  # not MANUAL: the exchange says our stop fired
    assert h.audit("fills.attributed")
    closing = h.db.all("SELECT role, order_id FROM fills WHERE trade_id = ? AND role != 'ENTRY'", (h.trade()["id"],))
    assert closing == [{"role": "SL", "order_id": h.orders("SL")[0]["id"]}]


async def test_exchange_side_cancels_with_alarming_reasons_alert_and_act(h):
    await h.open_protected_trade()
    tp2, tp3 = h.orders("TP")[1], h.orders("TP")[2]
    h.sim._handlers = StreamHandlers()
    h.sim._cancel(h.sim.orders[h.sim.by_cloid[tp2["cloid"]]], "marginCanceled")
    h.sim._cancel(h.sim.orders[h.sim.by_cloid[tp3["cloid"]]], "delistedCanceled")
    await h.until(lambda: h.db.get("orders", tp3["id"])["state"] == "CANCELED", timeout=5, message="cancels noticed")
    assert h.db.get("orders", tp2["id"])["exchange_status"] == "marginCanceled"
    assert any("cancelled by the exchange" in a and "marginCanceled" in a for a in h.alerts())
    assert h.health.has_condition(f"asset_blocked:{COIN}")  # delisted: the asset is blocked


async def test_an_order_the_exchange_no_longer_knows_is_recorded_as_missing(h):
    await h.open_protected_trade()
    tp5 = h.orders("TP")[4]
    h.sim._handlers = StreamHandlers()
    h.sim._cancel(h.sim.orders[h.sim.by_cloid[tp5["cloid"]]])
    h.sim.faults.hide_status_cloids.add(tp5["cloid"])
    await h.until(lambda: h.db.get("orders", tp5["id"])["state"] == "CANCELED", timeout=5, message="missing order handled")
    assert h.db.get("orders", tp5["id"])["exchange_status"] == "missing" and h.audit("order.missing")


async def test_closing_while_a_tp_fills_unnoticed_adopts_the_real_state_of_that_order(h):
    opened = await h.open_protected_trade()
    h.sim._handlers = StreamHandlers()  # the stream is silent
    h.freeze_background()
    await h.sim.set_quote(COIN, 4282.9, 4283.0, emit=False)  # TP1 fills; the bot has not seen it
    assert h.db.get("orders", h.orders("TP")[0]["id"])["state"] == "RESTING"
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.db.get("orders", h.orders("TP")[0]["id"])["state"] == "FILLED"  # cancel said "already filled": re-read, adopted
    assert h.sim.positions[COIN][0] == 0 and not h.sim.open_orders()
    assert h.db.scalar("SELECT COUNT(*) FROM fills WHERE role = 'TP'") == 1


async def test_mark_falls_back_to_rest_then_to_the_book_mid(h):
    await h.quote(4287.0)
    h.engine.contexts.clear()
    assert await h.engine._mark(COIN) == pytest.approx(4287.05)  # read over REST
    h.engine.contexts.clear()
    h.sim.faults.rest_down = True
    assert await h.engine._mark(COIN) == pytest.approx(4287.05)  # REST down: mid of the last quote
    h.engine.quotes.clear()
    assert await h.engine._mark(COIN) is None
    h.sim.faults.rest_down = False


async def test_unknown_close_order_is_resolved_before_another_close_is_sent(h):
    opened = await h.open_protected_trade()
    dropped = []

    def on_place(order):
        if order.reduce_only and order.tif == "Ioc" and order.kind == "limit" and not dropped:
            dropped.append(order.cloid)
            return "drop"  # the close went through; the response was lost
        return None

    h.sim.faults.on_place = on_place
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert len(h.orders("CLOSE")) == 1 and h.orders("CLOSE")[0]["state"] == "FILLED"
    assert h.sim.positions[COIN][0] == 0


async def test_request_close_on_unknown_or_finished_trades_is_refused(h):
    assert not h.engine.request_close(999)
    opened = await h.open_protected_trade()
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    assert not h.engine.request_close(opened["id"])
    assert not await h.engine.adopt(opened["id"]) and not h.engine.abort_entry(opened["id"])
    assert not await h.engine.modify_sl(opened["id"], trigger=4290.0)


async def test_kill_during_an_unresolved_entry_flattens_it_once_it_is_known_to_have_filled(h):
    import asyncio

    h.sim._handlers = StreamHandlers()
    h.sim.faults.on_place = lambda order: "drop" if entry(order) else None
    h.freeze_background()
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    h.engine._kill = True  # the operator hit kill while the entry outcome was unknown
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.until(lambda: h.trades() and h.trade()["state"] == "CLOSED", timeout=6, message="flattened")
    assert h.trade()["outcome"] == "FLATTENED" and h.trade()["close_reason"] == "kill"
    assert h.sim.positions[COIN][0] == 0
    await asyncio.sleep(0)


# ================================================================== startup (§9.6.8) and periodic checks
async def test_startup_fails_closed_when_instruments_cannot_be_read(make):
    h = await make(start=False)
    h.sim.faults.rest_down = True
    with pytest.raises(StartupError, match="instrument metadata"):
        await h.start()
    assert not h.app.started and not h.settings.pid_path.exists()
    h.sim.faults.rest_down = False
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    await h.start()  # the lock was released: a later start works
    assert h.app.started


async def test_startup_fails_when_no_configured_coin_exists(make):
    h = await make(start=False, symbol_map={"GOLD": "xyz:NOPE"})
    with pytest.raises(StartupError, match="none of the configured coins"):
        await h.start()


async def test_startup_fails_when_configured_leverage_exceeds_the_asset_maximum(make):
    h = await make(start=False, risk_leverage=26)
    with pytest.raises(StartupError, match="RISK_LEVERAGE 26 exceeds the maximum 25"):
        await h.start()


async def test_fr070_startup_fails_closed_when_the_signer_is_not_authorised(make):
    h = await make(start=False, env="testnet", account_address="0x" + "1a" * 20)
    h.sim.faults.signer_authorized = False
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    with pytest.raises(StartupError, match="not authorised"):
        await h.start()
    h.sim.faults.signer_authorized = True
    await h.start()
    assert h.app.started and h.settings.label == "TESTNET"
    await h.open_protected_trade()
    assert any("[TESTNET]" in t for t in h.sent)  # FR-081: records and notifications are labelled


async def test_sr02_agent_key_expiry_warns_at_14_days_and_pauses_inside_24_hours(make):
    h = await make(start=False, env="testnet", account_address="0x" + "1a" * 20)
    day = 24 * 3600 * 1000
    h.sim.faults.signer_expires_at_ms = h.clock.now_ms() + 10 * day
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    await h.start()
    assert sum("Agent key expires soon" in a for a in h.alerts()) == 1 and h.health.state() == "HEALTHY"
    await h.app.reconciler.check_signer()
    assert sum("Agent key expires soon" in a for a in h.alerts()) == 1  # once a day, not every check
    h.sim.faults.signer_expires_at_ms = h.clock.now_ms() + day // 2
    await h.app.reconciler.check_signer()
    assert h.health.has_condition("agent_key") and h.health.state() == "PAUSED"
    assert any("NEW agent key" in a for a in h.alerts())
    h.sim.faults.signer_authorized = False
    await h.app.reconciler.check_signer()
    assert "no longer authorised" in h.db.one("SELECT reason FROM conditions WHERE key = 'agent_key'")["reason"] or h.health.has_condition("agent_key")


async def test_rate_limit_headroom_is_checked_and_the_fee_floor_is_conservative(h):
    from hlbot.exchange.base import Limits

    assert h.engine.fee_rate == 0.0009  # never below the configured constant (IV-11)

    async def limits_low():
        return Limits(8500, 10000, 0.002, 0.0005)

    h.sim.get_limits = limits_low
    await h.app.reconciler.check_limits()
    assert h.engine.fee_rate == 0.002  # the account's real rate when it is higher
    assert any("headroom low" in a for a in h.alerts())

    async def limits_gone():
        return Limits(10000, 10000, 0.002, 0.0005)

    h.sim.get_limits = limits_gone
    await h.app.reconciler.check_limits()
    assert h.health.has_condition("rate_limit") and not h.health.can_enter(COIN)[0]

    async def limits_unreadable():
        raise OutcomeUnknown("timeout")

    h.engine.fee_rate = None
    h.sim.get_limits = limits_unreadable
    await h.app.reconciler.check_limits()
    assert h.engine.fee_rate == 0.0009


async def test_periodic_refreshes_run_from_the_reconcile_loop_and_survive_failures(h):
    h.app.reconciler._last_instruments = -1e9
    h.app.reconciler._last_limits = -1e9
    h.sim.faults.oi_capped.add(COIN)
    result = await h.app.reconciler.run_once("test")
    assert result["ok"] and h.health.has_condition(f"asset_blocked:{COIN}")
    h.sim.faults.oi_capped.clear()

    async def boom():
        raise ExchangeError(ErrorClass.OTHER, "meta unavailable")

    real = h.sim.get_instruments
    h.sim.get_instruments = boom
    await h.app.reconciler.refresh_instruments()  # outside startup a failed refresh is logged, not fatal
    assert h.engine.instruments[COIN].asset_id == 110003
    h.sim.get_instruments = real
    await h.app.reconciler.refresh_instruments()
    assert not h.health.has_condition(f"asset_blocked:{COIN}")
    assert h.app.reconciler.status()["last_ok_at"]


async def test_an_order_unresolved_long_past_its_deadline_pauses_entries(h):
    h.sim._handlers = StreamHandlers()
    h.sim.faults.on_place = lambda order: "lose" if entry(order) else None
    h.sim.faults.hide_status_cloids = set()
    h.freeze_background()
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    assert h.orders("ENTRY")[0]["state"] == "UNKNOWN"
    h.clock.advance(25.0, monotonic=False)
    h.app.reconciler._unknown_orders()
    assert h.health.has_condition("order_unknown") and h.health.state() == "PAUSED"
    h.db.update("orders", h.orders("ENTRY")[0]["id"], state="NOT_ACCEPTED")
    h.app.reconciler._unknown_orders()
    assert not h.health.has_condition("order_unknown")


async def test_recovery_that_cannot_complete_keeps_entries_disabled_and_alerts(make):
    h = await make()
    await h.quote(4287.0)
    h.sim._handlers = StreamHandlers()
    h.sim.faults.on_place = lambda order: "lose" if entry(order) else None
    h.freeze_background()
    signal_id = h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    await h.crash()

    h2 = Harness(h.tmp_path, sim=h.sim, clock=h.clock)
    h2.settings = None
    from hlbot.core.app import BotApp
    from tests.component.conftest import make_settings

    h2.settings = make_settings(h.tmp_path)
    h2.app = BotApp(h2.settings, adapter=h.sim, clock=h.clock, sender=h2._sender, fast_poll_s=0.02, watchdog_s=0.05, install_signal_handlers=False)
    real_startup = h2.app.__class__._start_locked
    import hlbot.core.reconciler as module

    original = module.Reconciler.startup

    async def quick(self, max_wait_s=0.3):
        return await original(self, max_wait_s=0.3)

    module.Reconciler.startup = quick
    try:
        await h2.app.start()  # the entry deadline has not passed: its outcome cannot be known yet
    finally:
        module.Reconciler.startup = original
    assert h2.health.has_condition("recovering") and not h2.health.can_enter(COIN)[0]
    assert any("Startup recovery incomplete" in a for a in h2.alerts())
    # Once the deadline passes, the periodic reconciliation finishes the job by itself.
    h.sim.faults.on_place = None
    h.clock.advance(11.0, monotonic=False)
    h.sim.touch()
    await h2.until(lambda: not h2.health.has_condition("recovering"), timeout=6, message="recovery completes later")
    assert h2.orders("ENTRY")[0]["state"] == "NOT_ACCEPTED" and h2.trades()[0]["state"] == "ABORTED"
    # The signal is still valid and in range, so it is re-evaluated with a fresh attempt.
    await h2.quote(4287.0)
    await h2.until(lambda: h2.signal(signal_id)["state"] == "TRADED", timeout=6, message="re-entered")
    assert h2.entry_requests() == 1
    del real_startup
    await h2.stop()


async def test_funding_payments_are_recorded_against_the_open_trade(h):
    from hlbot.exchange.base import FundingPayment

    opened = await h.open_protected_trade()
    await h.app._on_funding(FundingPayment(h.clock.now_ms(), COIN, -0.07, 0.0000125, -0.2412))
    await h.app._on_funding(FundingPayment(h.clock.now_ms(), COIN, -0.07, 0.0000125, -0.2412))  # replays are ignored
    row = h.db.one("SELECT * FROM funding_payments")
    assert row["trade_id"] == opened["id"] and row["usdc"] == -0.07 and h.db.scalar("SELECT COUNT(*) FROM funding_payments") == 1
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    trade = h.trade()
    assert trade["funding"] == -0.07 and trade["net_pnl"] == pytest.approx(trade["realized_pnl"] - trade["fees"] - 0.07)


async def test_a_dead_background_task_stops_the_process_so_the_supervisor_restarts_it(h):
    async def explode():
        raise RuntimeError("bug")

    h.app._spawn(explode(), "doomed")
    await h.until(lambda: h.app.stop_event.is_set(), message="stop requested")
    assert "doomed" in h.app.stop_reason and h.app.metrics.counter("task_crashes") == 1


async def test_telegram_network_failure_at_startup_is_an_outage_not_a_reason_to_exit(make):
    class Unreachable:
        connected = False
        resolved = []

        def __init__(self, settings, **callbacks):
            self.callbacks = callbacks

        async def connect(self):
            raise ConnectionError("network unreachable")

        async def run(self, stop):
            await stop.wait()

        async def disconnect(self):
            pass

    h = await make(start=False, telegram_enabled=True, telegram_channels=("@c",))
    h.settings = None
    from hlbot.core.app import BotApp
    from tests.component.conftest import make_settings

    h.settings = make_settings(h.tmp_path, telegram_enabled=True, telegram_channels=("@c",))
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    h.app = BotApp(h.settings, adapter=h.sim, clock=h.clock, sender=h._sender, telegram_factory=Unreachable,
                   fast_poll_s=0.02, watchdog_s=0.05, install_signal_handlers=False)
    await h.app.start()
    assert h.app.started and any("Telegram unreachable at startup" in a for a in h.alerts())


async def test_telegram_with_no_resolvable_channel_fails_startup(make):
    from hlbot.ingest.telegram import ChannelResolutionError

    class NoChannels:
        def __init__(self, settings, **callbacks):
            pass

        async def connect(self):
            raise ChannelResolutionError("none of the configured channels could be resolved")

        async def disconnect(self):
            pass

    h = await make(start=False)
    from hlbot.core.app import BotApp
    from tests.component.conftest import make_settings

    h.settings = make_settings(h.tmp_path, telegram_enabled=True, telegram_channels=("@c",))
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    h.app = BotApp(h.settings, adapter=h.sim, clock=h.clock, telegram_factory=NoChannels, install_signal_handlers=False)
    with pytest.raises(ChannelResolutionError):
        await h.app.start()
    assert not h.app.started


def test_settings_replace_keeps_risk_config_typed():
    from tests.component.conftest import make_settings

    settings = make_settings(__import__("pathlib").Path("/tmp/x"), risk_min_rr=0.9)
    assert settings.risk.min_rr == 0.9 and dataclasses.is_dataclass(settings.risk)


async def test_isolated_only_asset_is_blocked_under_cross_margin_with_a_clear_reason(h):
    h.sim.instruments[COIN] = dataclasses.replace(h.sim.instruments[COIN], only_isolated=True)
    await h.app.reconciler.refresh_instruments()
    allowed, why = h.health.can_enter(COIN)
    assert not allowed and "HL_MARGIN_MODE=isolated" in why
    h.sim.instruments[COIN] = dataclasses.replace(h.sim.instruments[COIN], only_isolated=False)
    await h.app.reconciler.refresh_instruments()
    assert h.health.can_enter(COIN)[0]


async def test_fill_sync_never_pages_the_whole_account_history(h):
    asked = []
    real = h.sim.get_fills

    async def spy(since_ms):
        asked.append(since_ms)
        return await real(since_ms)

    h.sim.get_fills = spy
    now = h.clock.now_ms()
    await h.engine.sync_fills()
    assert asked[-1] == now - 24 * 3600 * 1000  # nothing known yet: one day back
    await h.open_protected_trade()
    h.db.kv_set("last_fill_ms", 0)  # as if no fill had been stored for the trade yet
    await h.engine.sync_fills()
    assert asked[-1] == now - 10 * 60 * 1000  # from just before the first trade, not from zero
    h.db.kv_set("last_fill_ms", now)
    await h.engine.sync_fills()
    assert asked[-1] == now - 10 * 60 * 1000  # last stored fill minus the 10-minute overlap
    assert 0 not in asked


async def test_unfunded_dex_is_reported_as_no_collateral_not_as_a_daily_loss_and_recovers_when_funded(make):
    h = await make(start=False)
    h.sim.balance = 0.0
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    await h.start()
    await h.app.reconciler.run_once("test")
    assert h.health.has_condition(f"no_collateral:{COIN}") and not h.health.has_condition("daily_loss")
    allowed, why = h.health.can_enter(COIN)
    assert not allowed and "no collateral on the xyz perp dex" in why
    assert h.db.scalar("SELECT COUNT(*) FROM day_state") == 0  # no percentage base is invented from zero
    assert sum("no collateral" in a for a in h.alerts()) == 1

    # A day row left at zero by an earlier version is re-based, and its bogus marker dropped.
    h.db.execute("INSERT INTO day_state (utc_date, start_equity, loss_limit_hit_at) VALUES (?, 0, ?)", (h.clock.utc_date(), h.clock.iso()))
    h.health.set_condition("daily_loss", "left over", level="block", auto_resume=True, alert=False)
    h.sim.balance = 500.0  # the operator funds the dex
    await h.app.reconciler.run_once("test")
    assert not h.health.has_condition(f"no_collateral:{COIN}") and not h.health.has_condition("daily_loss")
    row = h.db.one("SELECT * FROM day_state")
    assert row["start_equity"] == 500.0 and row["loss_limit_hit_at"] is None
    assert h.health.can_enter(COIN)[0]
    await h.open_protected_trade()


async def test_self_clearing_conditions_are_rederived_at_startup_and_manual_ones_survive(h):
    """Found on testnet: a block about a market that was no longer
    configured lingered after a restart and stopped every entry."""
    h.health.set_condition("no_collateral:par:GOLD", "left from a previous configuration", level="block", scope="par:GOLD", auto_resume=True, alert=False)
    h.health.set_condition("no_collateral:par", "older, global form of the same thing", level="block", auto_resume=True, alert=False)
    h.health.set_condition(f"protection_failure:{COIN}", "needs the operator", level="pause", scope=COIN, auto_resume=False, alert=False)
    assert not h.health.can_enter(COIN)[0]
    await h.crash()
    h2 = await h.restart()
    keys = {c["key"] for c in h2.health.conditions()}
    assert keys == {f"protection_failure:{COIN}"}  # manual-resume conditions persist across restarts
    assert h2.audit("health.auto_conditions_reset")
    h2.health.resume()
    assert h2.health.can_enter(COIN)[0]
    await h2.stop()


async def test_no_collateral_on_one_dex_does_not_block_a_market_on_another(make):
    h = await make(start=False, symbol_map={"GOLD": COIN, "PAXG": "PAXG"})
    h.sim.add_instrument("PAXG", sz_decimals=3, max_leverage=10, asset_id=165)
    await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
    await h.sim.set_quote("PAXG", 4210.0, 4210.5, emit=False)
    await h.start()
    real = h.sim.get_account

    async def xyz_unfunded():
        import dataclasses as dc

        account = await real()
        by_dex = dict(account.by_dex)
        by_dex["xyz"] = dc.replace(by_dex["xyz"], account_value=0.0, withdrawable=0.0)
        return dc.replace(account, by_dex=by_dex)

    h.sim.get_account = xyz_unfunded
    await h.app.reconciler.run_once("test")
    assert not h.health.can_enter(COIN)[0] and "no collateral on the xyz perp dex" in h.health.can_enter(COIN)[1]
    assert h.health.can_enter("PAXG")[0]
