"""Unknown outcomes, crash recovery, single instance (FR-047, FR-057,
FR-059, FR-061; TC-050, TC-051, TC-070..TC-074, TC-076, TC-101)."""

import pytest

from hlbot.core.app import AlreadyRunning, BotApp
from tests.component.conftest import COIN, SELL_TEXT, Harness, make_settings


class CrashNow(BaseException):
    """Stands in for the process being killed at an exact point."""


def is_entry(order):
    return not order.reduce_only


def is_sl(order):
    return order.kind == "trigger" and order.tpsl == "sl"


# ---------------------------------------------------------------- unknown outcomes (§9.6.4)


async def test_tc050_entry_times_out_but_was_accepted_resolved_by_cloid_protected_no_second_entry(h):
    """The response is lost AND the fill stream is silent: only the
    lookup by client order id can settle it."""
    from hlbot.exchange.base import StreamHandlers

    h.sim._handlers = StreamHandlers()  # no stream events reach the bot
    h.sim.faults.on_place = lambda order: "drop" if is_entry(order) else None
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", timeout=6, message="PROTECTED after resolution")
    assert h.signal(signal_id)["state"] == "TRADED"
    assert h.entry_requests() == 1 and len(h.trades()) == 1  # no second entry
    entry = h.orders("ENTRY")[0]
    assert entry["state"] == "FILLED"
    states = [e["payload"] for e in h.audit("order.state") if e["order_id"] == entry["id"]]
    assert any('"to": "UNKNOWN"' in p for p in states)  # unknown was a recorded state, not a retry
    assert h.audit("order.resolved")
    assert h.sim.positions[COIN][0] == pytest.approx(-h.trade()["filled_size"])
    assert h.trade()["avg_entry"] == 4287.0 and h.db.scalar("SELECT COUNT(*) FROM fills WHERE role = 'ENTRY'") == 1


async def test_tc050_variant_the_fill_stream_settles_a_lost_response_before_the_lookup(h):
    h.sim.faults.on_place = lambda order: "drop" if is_entry(order) else None
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", timeout=6, message="PROTECTED")
    assert h.signal(signal_id)["state"] == "TRADED" and h.entry_requests() == 1 and len(h.trades()) == 1
    assert h.orders("ENTRY")[0]["state"] == "FILLED"
    assert h.app.metrics.counter("unknown_outcomes") == 1


async def test_tc051_entry_times_out_and_was_not_accepted_is_not_accepted_after_the_deadline_then_reevaluated(h):
    lost = []

    def on_place(order):
        if is_entry(order) and not lost:
            lost.append(order.cloid)
            return "lose"
        return None

    h.sim.faults.on_place = on_place
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    # Before expiresAfter + 5 s the outcome stays UNKNOWN and nothing is retried.
    assert h.orders("ENTRY")[0]["state"] == "UNKNOWN" and h.signal(signal_id)["state"] == "ENTERING"
    await h.quote(4287.0)
    await h.settle()
    assert len(h.orders("ENTRY")) == 1 and h.entry_requests() == 0

    h.clock.advance(11.0, monotonic=False)  # past the entry deadline + grace
    h.sim.touch()
    await h.until(lambda: h.orders("ENTRY")[0]["state"] == "NOT_ACCEPTED", message="NOT_ACCEPTED")
    assert h.trades()[0]["state"] == "ABORTED"
    await h.quote(4287.0)  # still in range and unexpired: re-evaluated with attempt + 1
    await h.until(lambda: h.trade()["state"] == "PROTECTED", timeout=6, message="re-entered")
    entries = h.orders("ENTRY")
    assert len(entries) == 2 and entries[1]["attempt"] == 2 and entries[0]["cloid"] != entries[1]["cloid"]
    assert h.entry_requests() == 1  # only the second request ever reached the exchange
    assert h.signal(signal_id)["state"] == "TRADED" and h.signal(signal_id)["attempts"] == 2


async def test_unknown_entry_with_position_but_no_order_or_fill_is_unreconciled_and_pauses(h):
    """§9.6.4 step 5: contradictory evidence."""
    h.sim.faults.on_place = lambda order: "lose" if is_entry(order) else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.sim.external_trade(COIN, False, 0.05)  # a position appears that no order of ours explains
    h.db.execute("DELETE FROM fills")  # (the foreign fill is not ours; keep the scenario focused on the lookup)
    h.clock.advance(11.0, monotonic=False)
    h.sim.touch()
    await h.until(lambda: h.orders("ENTRY")[0]["state"] == "UNRECONCILED", message="UNRECONCILED")
    assert h.health.has_condition("order_unreconciled") and h.health.state() == "PAUSED"
    assert h.entry_requests() == 0
    # The operator confirms the bot does not own the position.
    result = await h.app.command("reconcile", {"close": True, "trade_id": h.trade()["id"]})
    assert result["ok"] and h.trade()["state"] == "ABORTED" and h.orders("ENTRY")[0]["state"] == "CANCELED"
    assert not h.health.has_condition("order_unreconciled")
    assert h.sim.positions[COIN][0] == pytest.approx(-0.05)  # the foreign position was never touched


# ---------------------------------------------------------------- crash injection


async def test_tc070_kill_before_send_zero_orders_and_the_signal_is_reevaluated(h):
    async def die(*args, **kwargs):
        raise CrashNow()

    h.engine._send = die  # the process dies after the intent is committed, before the request leaves
    h.freeze_background()
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.settle()
    assert h.orders("ENTRY")[0]["state"] == "INTENT" and h.trade()["state"] == "ENTERING"
    await h.crash()
    assert not [r for r in h.sim.requests if r["kind"] == "order"]  # nothing reached the exchange

    h.clock.advance(11.0, monotonic=False)
    h.sim.touch()
    h2 = await h.restart()
    assert h2.orders("ENTRY")[0]["state"] == "NOT_ACCEPTED" and h2.trades()[0]["state"] == "ABORTED"
    assert h2.signal(signal_id)["state"] == "WAITING_PRICE"
    assert h2.app.reconciler.recovery_invariant_ok() and not h2.health.has_condition("recovering")
    await h2.quote(4287.0)
    await h2.until(lambda: h2.trade()["state"] == "PROTECTED", timeout=6, message="entered after recovery")
    assert h2.entry_requests() == 1
    await h2.stop()


async def test_tc071_kill_after_send_before_response_exactly_one_entry_protected_after_recovery(h):
    import asyncio

    from hlbot.exchange.base import StreamHandlers

    real_place = h.sim.place

    async def place_then_die(orders, nonce, expires_after=None):
        await real_place(orders, nonce, expires_after)  # the exchange accepts and fills the entry...
        raise CrashNow()  # ...and the process dies before it reads the response

    h.sim.place = place_then_die
    h.sim._handlers = StreamHandlers()  # a dead process receives no stream events either
    h.freeze_background()
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await asyncio.sleep(0.3)
    assert h.orders("ENTRY")[0]["state"] == "SUBMITTING" and h.trade()["state"] == "ENTERING"
    assert h.sim.positions[COIN][0] < 0 and not h.sim.open_orders()  # filled and UNPROTECTED on the exchange
    await h.crash()

    h.sim.place = real_place
    h2 = await h.restart()
    # Recovery resolved the entry by cloid and protected the position before enabling entries.
    assert h2.trade()["state"] == "PROTECTED" and h2.signal(signal_id)["state"] == "TRADED"
    assert h2.orders("ENTRY")[0]["state"] == "FILLED" and len(h2.orders("ENTRY")) == 1
    assert h2.entry_requests() == 1  # exactly one entry order ever reached the exchange
    assert h2.orders("SL")[0]["cloid"] in {o.cloid for o in h2.sim.open_orders()}
    assert h2.engine.entries_enabled and h2.app.reconciler.recovery_invariant_ok()
    # It was settled from the exchange's own records (the fill carries our cloid), not from a retry.
    fills = h2.audit("fill")
    assert len(fills) == 1 and '"source": "rest"' in fills[0]["payload"] and '"role": "ENTRY"' in fills[0]["payload"]
    await h2.stop()


async def test_tc072_kill_between_fill_and_sl_the_sl_is_placed_first_on_recovery(h):
    async def die(trade):
        raise CrashNow()

    h.engine._protect = die  # entry confirmed and committed as OPEN_UNPROTECTED, then the process dies
    h.freeze_background()
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    assert h.trade()["state"] == "OPEN_UNPROTECTED" and not h.orders("SL")
    await h.crash()

    h2 = await h.restart()
    assert h2.trade()["state"] == "PROTECTED"
    assert h2.orders("SL")[0]["cloid"] in {o.cloid for o in h2.sim.open_orders()}
    # Protection came before anything else: the first order request after the restart is the SL.
    after_restart = [r for r in h2.sim.requests if r["kind"] == "order"][1:]
    assert after_restart[0]["detail"] == [h2.orders("SL")[0]["cloid"]]
    assert len(h2.orders("TP")) == 5 and h2.entry_requests() == 1
    await h2.stop()


async def test_tc072_recovery_flattens_when_the_position_cannot_be_protected(h):
    async def die(trade):
        raise CrashNow()

    h.engine._protect = die
    h.freeze_background()
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    await h.crash()
    h.sim.faults.on_place = lambda order: "reject:Invalid TP/SL price." if is_sl(order) else None
    h2 = await h.restart()
    await h2.until(lambda: h2.trade()["state"] == "CLOSED", timeout=6, message="flattened on recovery")
    assert h2.trade()["outcome"] == "FLATTENED" and h2.sim.positions[COIN][0] == 0
    await h2.stop()


async def test_tc073_kill_while_protected_the_trade_is_adopted_with_no_new_orders(h):
    await h.open_protected_trade()
    requests_before = len(h.sim.requests)
    open_before = sorted(o.cloid for o in h.sim.open_orders())
    await h.crash()

    h2 = await h.restart()
    assert h2.trade()["state"] == "PROTECTED"
    assert len(h2.sim.requests) == requests_before  # nothing was sent
    assert sorted(o.cloid for o in h2.sim.open_orders()) == open_before
    assert h2.engine.entries_enabled
    # ...and it is still managed: a TP fill after the restart is processed normally.
    await h2.quote(4282.9, 4283.0)
    await h2.until(lambda: h2.trade()["state"] == "REDUCING", message="REDUCING after restart")
    await h2.stop()


async def test_tc074_kill_mid_ladder_missing_tps_are_placed_and_none_duplicated(h):
    original = h.engine._ensure_tps

    async def partial_ladder(trade):
        raise CrashNow()

    # Let the SL through, then die before any TP is sent.
    h.engine._ensure_tps = partial_ladder
    h.freeze_background()
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.settle()
    assert h.orders("SL") and not h.orders("TP")
    del original
    await h.crash()

    # Half the ladder made it out before the crash: place TP1 and TP2 the way the engine would have.
    h2 = Harness(h.tmp_path, sim=h.sim, clock=h.clock)
    seen = []
    h.sim.faults.on_place = lambda order: seen.append(order.cloid) or None
    await h2.start()
    await h2.until(lambda: h2.trade()["state"] == "PROTECTED" and len(h2.orders("TP")) == 5, timeout=6, message="ladder completed")
    tps = h2.orders("TP")
    assert sorted(o["leg"] for o in tps) == [1, 2, 3, 4, 5]
    assert len({o["cloid"] for o in tps}) == 5 and len(seen) == len(set(seen))  # none duplicated
    on_exchange = [o for o in h2.sim.open_orders() if not o.is_trigger]
    assert len(on_exchange) == 5 and sum(o.size for o in on_exchange) == pytest.approx(h2.trade()["filled_size"])
    assert len(h2.orders("SL")) == 1
    await h2.stop()


async def test_tc074_tp_intents_persisted_but_unsent_are_resolved_not_blindly_resent(h):
    await h.quote(4287.0)
    sent_tp = []
    real_send = h.engine._send

    async def send(trade_id, orders, priority, expires_after=None):
        if orders and orders[0]["role"] == "TP":
            sent_tp.append([o["cloid"] for o in orders])
            raise CrashNow()  # TP intents are committed; the request never leaves
        return await real_send(trade_id, orders, priority, expires_after)

    h.engine._send = send
    h.freeze_background()
    h.post(SELL_TEXT)
    await h.settle()
    assert [o["state"] for o in h.orders("TP")] == ["INTENT"] * 5
    await h.crash()

    h2 = await h.restart()
    await h2.until(lambda: h2.trade()["state"] == "PROTECTED" and len([o for o in h2.sim.open_orders() if not o.is_trigger]) == 5, timeout=6, message="ladder on exchange")
    tps = h2.orders("TP")
    assert [o["state"] for o in tps[:5]] == ["NOT_ACCEPTED"] * 5  # looked up by cloid first
    assert [o["attempt"] for o in tps[5:]] == [2] * 5 and len(tps) == 10
    assert len([o for o in h2.sim.open_orders() if not o.is_trigger]) == 5  # exactly one live order per leg
    await h2.stop()


async def test_recovery_blocks_entries_until_it_completes(h):
    """FR-059: no entry is sent before recovery completes."""
    await h.open_protected_trade()
    await h.crash()
    h2 = Harness(h.tmp_path, sim=h.sim, clock=h.clock)
    h2.settings = make_settings(h.tmp_path)
    app = BotApp(h2.settings, adapter=h.sim, clock=h.clock, sender=h2._sender, fast_poll_s=0.02, watchdog_s=0.05, install_signal_handlers=False)
    h2.app = app
    assert app.started is False
    await app.start()
    events = [e["kind"] for e in h2.audit()]
    assert events.index("recovery.complete") < events.index("service.started")
    await h2.stop()


async def test_tc076_fr061_a_second_instance_refuses_to_start(h):
    second = BotApp(make_settings(h.tmp_path), adapter=h.sim, clock=h.clock, install_signal_handlers=False)
    with pytest.raises(AlreadyRunning, match="double-send"):
        await second.start()
    assert h.app.started  # the first instance is unaffected and its lock is intact
    third = BotApp(make_settings(h.tmp_path), adapter=h.sim, clock=h.clock, install_signal_handlers=False)
    with pytest.raises(AlreadyRunning):
        await third.start()


async def test_tc090_pause_persists_across_restart_and_blocks_entries_until_resume(h):
    await h.quote(4300.0)
    signal_id = h.post(SELL_TEXT)
    await h.app.command("pause", {"reason": "maintenance"})
    await h.quote(4287.0)
    assert h.signal(signal_id)["state"] == "WAITING_PRICE" and not h.sim.requests
    await h.crash()

    h2 = await h.restart()
    assert h2.health.operator_paused() and h2.health.state() == "PAUSED"
    await h2.quote(4287.0)
    assert not h2.trades() and h2.entry_requests() == 0
    await h2.app.command("resume")
    await h2.quote(4287.0)
    await h2.until(lambda: h2.trades() and h2.trade()["state"] == "PROTECTED", timeout=6, message="entered after resume")
    await h2.stop()


async def test_tc101_daily_loss_limit_survives_a_restart(make):
    h = await make()
    await h.open_protected_trade()
    await h.quote(4296.0, 4296.1)  # stopped out: about -0.5 % of equity
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    assert h.trade()["net_pnl"] < 0
    await h.crash()

    # Restart with a daily loss limit tighter than the loss already realised today.
    h2 = Harness(h.tmp_path, sim=h.sim, clock=h.clock, risk_max_daily_loss_pct=0.3)
    h2._next_message = h._next_message
    await h2.start()
    await h2.app.reconciler.run_once("test")
    assert h2.health.has_condition("daily_loss") and not h2.health.can_enter(COIN)[0]
    signal_id = h2.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h2.quote(4287.0)
    await h2.settle()
    assert h2.signal(signal_id)["state"] == "WAITING_PRICE" and len(h2.trades()) == 1  # still blocked
    # The next UTC day clears it automatically.
    h2.clock.advance(24 * 3600, monotonic=False)
    h2.sim.touch()
    await h2.app.reconciler.run_once("test")
    assert not h2.health.has_condition("daily_loss")
    await h2.stop()
