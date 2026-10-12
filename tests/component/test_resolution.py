"""Order-resolution and cleanup branches: every way an order of unknown
or stale state is brought back in line with the exchange (PRD §9.6.4,
§12.5), and what happens when the exchange refuses to cooperate."""

import pytest

from hlbot.exchange.base import ErrorClass, ExchangeError, FundingPayment, OutcomeUnknown, StreamHandlers
from tests.component.conftest import COIN, SELL_TEXT


def entry(order):
    return not order.reduce_only


def tp(order):
    return order.reduce_only and order.kind == "limit" and order.tif == "Gtc"


async def silent(h):
    """No stream events and no background healing: only explicit drives act."""
    h.sim._handlers = StreamHandlers()
    h.freeze_background()


async def test_unknown_tp_orders_found_resting_are_adopted_without_resending(h):
    h.sim.faults.on_place = lambda order: "drop" if tp(order) else None  # the ladder went through; the response was lost
    await h.open_protected_trade()
    await h.until(lambda: all(o["state"] == "RESTING" for o in h.orders("TP")), timeout=5, message="TPs adopted")
    assert len(h.orders("TP")) == 5 and len([o for o in h.sim.open_orders() if not o.is_trigger]) == 5
    assert all(o["oid"] for o in h.orders("TP"))


async def test_unknown_order_found_cancelled_or_triggered_takes_that_state(h):
    opened = await h.open_protected_trade()
    await silent(h)
    tp4, tp5 = h.orders("TP")[3], h.orders("TP")[4]
    for row in (tp4, tp5):
        h.db.update("orders", row["id"], state="UNKNOWN")
    h.sim._cancel(h.sim.orders[h.sim.by_cloid[tp4["cloid"]]], "selfTradeCanceled")
    h.sim.orders[h.sim.by_cloid[tp5["cloid"]]].status = "triggered"
    assert await h.engine._resolve_unknown(h.db.get("orders", tp4["id"])) == "found"
    assert await h.engine._resolve_unknown(h.db.get("orders", tp5["id"])) == "found"
    assert h.db.get("orders", tp4["id"])["state"] == "CANCELED" and h.db.get("orders", tp4["id"])["exchange_status"] == "selfTradeCanceled"
    assert h.db.get("orders", tp5["id"])["state"] == "TRIGGERED"
    assert opened["id"] == h.trade()["id"]


async def test_unknown_partial_ioc_found_cancelled_keeps_its_fill_and_ends_cancelled(h):
    await silent(h)
    h.sim.faults.on_place = lambda order: "partial:0.5" if entry(order) else None
    real = h.sim.place

    async def place(orders, nonce, expires_after=None):
        results = await real(orders, nonce, expires_after)
        if any(entry(o) for o in orders):
            raise OutcomeUnknown("response lost")  # the half-filled IOC went through; the response did not
        return results

    h.sim.place = place
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    h.sim.place = real
    h.sim.faults.on_place = None
    await h.until(lambda: h.trade()["state"] == "PROTECTED", timeout=6, message="PROTECTED on the partial fill")
    order = h.orders("ENTRY")[0]
    assert order["state"] == "CANCELED" and 0 < order["filled_size"] < order["size"]
    assert h.trade()["filled_size"] == pytest.approx(order["filled_size"])


async def test_unknown_order_the_status_endpoint_cannot_find_is_settled_from_its_fills(h):
    await silent(h)
    h.sim.faults.on_place = lambda order: "drop" if entry(order) else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    cloid_seen = []
    h.sim.faults.hide_status_cloids = set()
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    order = h.orders("ENTRY")[0]
    cloid_seen.append(order["cloid"])
    if h.trade()["state"] == "ENTERING":
        h.sim.faults.hide_status_cloids.add(order["cloid"])  # orderStatus says unknownOid although it filled
        h.clock.advance(11.0, monotonic=False)
        h.sim.touch()
        assert await h.engine._resolve_unknown(h.db.get("orders", order["id"])) == "found"
    await h.until(lambda: h.orders("ENTRY")[0]["state"] == "FILLED", timeout=6, message="settled from fills")
    assert h.entry_requests() == 1


async def test_unreconciled_entry_is_settled_when_its_fills_arrive(h):
    await silent(h)
    h.sim.faults.on_place = lambda order: "drop" if entry(order) else None
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    real_send = h.engine._send

    async def send_then_forget(trade_id, orders, priority, expires_after=None):
        result = await real_send(trade_id, orders, priority, expires_after)
        return result

    h.engine._send = send_then_forget
    # Make the first lookup see nothing at all: status hidden, fills hidden.
    real_fills = h.sim.get_fills

    async def no_fills(since):
        return []

    h.sim.get_fills = no_fills
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    order = h.orders("ENTRY")[0]
    h.sim.faults.hide_status_cloids.add(order["cloid"])
    h.db.update("orders", order["id"], state="UNKNOWN", filled_size=0)
    h.db.execute("UPDATE trades SET state = 'ENTERING', filled_size = 0, remaining_size = 0 WHERE id = ?", (h.trade()["id"],))
    h.clock.advance(11.0, monotonic=False)
    h.sim.touch()
    assert await h.engine._resolve_unknown(h.db.get("orders", order["id"])) == "unreconciled"
    assert h.db.get("orders", order["id"])["state"] == "UNRECONCILED" and h.health.has_condition("order_unreconciled")

    # The fills become visible: the contradiction resolves itself and the position is protected.
    h.sim.get_fills = real_fills
    h.db.execute("DELETE FROM fills")
    h.engine._ensure_loop(h.trade()["id"])
    await h.engine.sync_fills()
    await h.until(lambda: h.trade()["state"] in ("PROTECTED", "REDUCING"), timeout=6, message="protected after the fills arrived")
    assert not h.health.has_condition("order_unreconciled")
    assert h.orders("ENTRY")[0]["state"] == "FILLED" and h.entry_requests() == 1


async def test_unreconciled_entry_is_cancelled_when_the_account_turns_out_flat(h):
    await silent(h)
    h.sim.faults.on_place = lambda order: "lose" if entry(order) else None
    await h.quote(4287.0)
    signal_id = h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.settle()
    order = h.orders("ENTRY")[0]
    h.db.update("orders", order["id"], state="UNRECONCILED")  # as left by an earlier contradiction
    h.health.set_condition("order_unreconciled", "test", level="pause", auto_resume=False, alert=False)
    await h.engine.drive(h.trade()["id"])
    assert h.db.get("orders", order["id"])["state"] == "CANCELED" and h.trades()[0]["state"] == "ABORTED"
    assert not h.health.has_condition("order_unreconciled")
    assert h.signal(signal_id)["state"] in ("WAITING_PRICE", "ENTERING", "TRADED")


async def test_position_gone_on_the_exchange_without_any_fill_closes_the_trade_as_manual(h):
    opened = await h.open_protected_trade()
    await silent(h)
    h.sim.positions[COIN] = [0.0, 0.0]  # the position vanished and no fill explains it
    await h.engine.drive(opened["id"])
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.trade()["outcome"] == "MANUAL" and h.trade()["close_reason"] == "external"
    assert h.audit("trade.closed_externally") and not h.sim.open_orders()


async def test_unexplained_size_difference_is_unreconciled_and_adopt_accepts_the_exchange_size(h):
    opened = await h.open_protected_trade()
    await silent(h)
    actual = round(opened["filled_size"] - 0.03, 4)
    h.sim.positions[COIN][0] = -actual  # smaller than the fills say, with no fill to explain it
    await h.engine.drive(opened["id"])
    assert h.trade()["state"] == "UNRECONCILED" and h.health.has_condition(f"recon_divergence:{COIN}")
    assert "differs from expected" in h.db.one("SELECT reason FROM conditions WHERE key LIKE 'recon_divergence%'")["reason"]
    assert await h.engine.adopt(opened["id"])
    assert h.trade()["remaining_size"] == pytest.approx(actual) and h.trade()["state"] in ("PROTECTED", "REDUCING")
    assert not h.health.has_condition(f"recon_divergence:{COIN}")


async def test_sl_modify_with_unknown_outcome_is_resolved_from_the_exchange_and_failure_keeps_the_old_stop(h):
    opened = await h.open_protected_trade()
    await silent(h)
    real = h.sim.modify

    async def lost(cloid, new, nonce):
        await real(cloid, new, nonce)
        raise OutcomeUnknown("response lost")

    h.sim.modify = lost
    assert not await h.engine.modify_sl(opened["id"], trigger=4294.0, reason="operator")
    assert h.orders("SL")[0]["state"] == "UNKNOWN"
    await h.engine.drive(opened["id"])  # the next verification reads the truth from the exchange
    sl = h.orders("SL")[0]
    assert sl["state"] == "RESTING" and sl["trigger_price"] == 4294.0

    async def refused(cloid, new, nonce):
        raise ExchangeError(ErrorClass.OTHER, "modify unavailable")

    h.sim.modify = refused
    assert not await h.engine.modify_sl(opened["id"], trigger=4292.0, reason="operator")
    assert h.orders("SL")[0]["state"] == "RESTING" and h.orders("SL")[0]["trigger_price"] == 4294.0
    assert any("previous stop kept" in a for a in h.alerts())
    assert not await h.engine.modify_sl(opened["id"], size=0.01)  # never shrink the stop below the position
    h.sim.modify = real


async def test_a_failed_cancel_request_leaves_orders_resting_and_is_retried(h):
    opened = await h.open_protected_trade()
    real = h.sim.cancel
    failures = []

    async def flaky(coin, cloids, nonce):
        if len(failures) < 2:
            failures.append(1)
            raise ExchangeError(ErrorClass.OTHER, "cancel endpoint error")
        return await real(coin, cloids, nonce)

    h.sim.cancel = flaky
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED once cancels go through")
    assert len(failures) == 2 and not h.sim.open_orders()


async def test_cancel_rejected_for_another_reason_keeps_the_order_and_reports_a_stuck_leftover(h):
    opened = await h.open_protected_trade()
    h.sim.faults.cancel_text = "Cancels are temporarily disabled"
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSING" and h.sim.positions[COIN][0] == 0, timeout=6, message="flat, cleaning up")
    assert h.sim.open_orders()  # the exchange would not cancel them; they are reduce-only and cannot open exposure
    h.clock.advance(61.0)  # FR-055 / §10.1 stage 10: alert if still open after 60 s
    h.engine.wake(opened["id"])
    await h.until(lambda: any("Leftover order will not cancel" in a for a in h.alerts()), timeout=6, message="leftover alert")
    h.sim.faults.cancel_text = None
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED after the exchange accepts cancels")
    assert not h.sim.open_orders()


async def test_a_bot_order_open_on_the_exchange_but_finished_locally_is_cancelled_during_cleanup(h):
    opened = await h.open_protected_trade()
    await silent(h)
    tp5 = h.orders("TP")[4]
    h.db.update("orders", tp5["id"], state="NOT_ACCEPTED")  # we gave up on it, yet it landed
    h.engine.request_close(opened["id"], "manual")
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert tp5["cloid"] not in {o.cloid for o in h.sim.open_orders()} and not h.sim.open_orders()


async def test_close_requested_while_unprotected_goes_through_flatten(h):
    await silent(h)

    async def never(trade):
        return None

    h.engine._protect = never  # hold the trade in OPEN_UNPROTECTED
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.until(lambda: h.trades() and h.trade()["state"] == "OPEN_UNPROTECTED", message="OPEN_UNPROTECTED")
    assert h.engine.request_close(h.trade()["id"], "manual")
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    assert h.sim.positions[COIN][0] == 0 and h.trade()["close_reason"] == "manual"
    assert h.engine.request_close(h.trades()[0]["id"]) is False


async def test_rest_failure_during_a_close_is_retried_not_assumed(h):
    opened = await h.open_protected_trade()
    h.sim.faults.rest_down = True
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.settle()
    assert h.trade()["state"] == "CLOSING" and h.sim.positions[COIN][0] != 0 and not h.orders("CLOSE")
    h.sim.faults.rest_down = False
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED once REST is back")


async def test_funding_history_is_fetched_at_close_and_included_in_net_pnl(h):
    opened = await h.open_protected_trade()

    async def funding(since_ms):
        return [FundingPayment(h.clock.now_ms(), COIN, -0.11, 0.0000125, -0.24), FundingPayment(h.clock.now_ms(), "PAXG", -9.0)]

    h.sim.get_funding = funding
    await h.app.command("close", {"trade_id": opened["id"]})
    await h.until(lambda: h.trade()["state"] == "CLOSED", timeout=6, message="CLOSED")
    trade = h.trade()
    assert trade["funding"] == -0.11  # only this coin's payments, to the cent
    assert trade["net_pnl"] == pytest.approx(trade["realized_pnl"] - trade["fees"] - 0.11, abs=1e-6)

    async def unavailable(since_ms):
        raise OutcomeUnknown("timeout")

    h.sim.get_funding = unavailable
    assert await h.engine._sync_funding(trade) == -0.11  # an unavailable history does not lose what is recorded


async def test_recovery_resolves_an_sl_intent_that_was_persisted_but_never_sent(h):
    await silent(h)
    real_send = h.engine._send

    class Die(BaseException):
        pass

    async def send(trade_id, orders, priority, expires_after=None):
        if orders and orders[0]["role"] == "SL":
            raise Die()
        return await real_send(trade_id, orders, priority, expires_after)

    h.engine._send = send
    await h.quote(4287.0)
    h.post(SELL_TEXT)
    await h.engine.on_quote(await h.sim.get_book(COIN))
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    await h.until(lambda: h.orders("SL") and h.orders("SL")[0]["state"] == "INTENT", message="SL intent persisted")
    await h.crash()
    h2 = await h.restart()
    await h2.until(lambda: h2.trade()["state"] == "PROTECTED", timeout=6, message="PROTECTED")
    sls = h2.orders("SL")
    assert [o["state"] for o in sls] == ["NOT_ACCEPTED", "RESTING"] and sls[1]["attempt"] == 2
    assert sum(1 for o in h2.sim.open_orders() if o.is_trigger) == 1
    await h2.stop()
