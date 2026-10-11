"""Randomised crash injection (component-level analogue of TC-075 /
NFR-007): kill the process at 100 random points of the entry -> protect
-> ladder sequence, restart on the same database against the same
exchange, and check the two invariants that matter:

* zero duplicate entries: at most one entry ever fills for a signal;
* no position is left unprotected: after recovery an open position is
  covered by a verified stop-loss, or it has been flattened.

This proves the recovery logic against the simulated exchange. It does
not replace the testnet run the PRD asks for in gate G2.
"""

import asyncio
import random

import pytest

from tests.component.conftest import COIN, SELL_TEXT, Harness

KILLS = 100
WRAPPED = ("place", "cancel", "modify", "get_order", "get_fills", "get_open_orders", "get_account", "get_leverage", "set_leverage")


class Killed(BaseException):
    """The process is dead: nothing after this point runs."""


def arm(sim, kill_at: int, after: bool):
    """Die at the ``kill_at``-th exchange call, either just before the
    request leaves or just after the exchange has processed it."""
    state = {"calls": 0, "dead": False}
    originals = {name: getattr(sim, name) for name in WRAPPED}

    def wrap(name):
        real = originals[name]

        async def call(*args, **kwargs):
            if state["dead"]:
                raise Killed()
            state["calls"] += 1
            if state["calls"] == kill_at and not after:
                state["dead"] = True
                raise Killed()
            result = await real(*args, **kwargs)
            if state["calls"] == kill_at and after:
                state["dead"] = True
                raise Killed()
            return result

        return call

    for name in WRAPPED:
        setattr(sim, name, wrap(name))

    def disarm():
        for name, real in originals.items():
            setattr(sim, name, real)

    return state, disarm


async def one_kill(tmp_path, rng: random.Random, index: int) -> dict:
    from hlbot.exchange.base import StreamHandlers

    h = Harness(tmp_path / f"run{index}")
    await h.sim.set_quote(COIN, 4287.0, 4287.1, emit=False)
    await h.start()
    baseline = len(h.sim.requests)
    kill_at = rng.randint(1, 14)  # the whole sequence makes about 12 exchange calls
    after = rng.random() < 0.5
    stream_alive = rng.random() < 0.5
    h.freeze_background()
    if not stream_alive:
        h.sim._handlers = StreamHandlers()
    state, disarm = arm(h.sim, kill_at, after)
    signal_id = h.post(SELL_TEXT)
    await h.engine.on_quote(h.sim.quotes[COIN])
    await h.app.signals.on_quote(h.sim.quotes[COIN])
    for _ in range(40):
        if state["dead"]:
            break
        await asyncio.sleep(0.01)
    killed = state["dead"]
    state["dead"] = True  # whatever is still running is gone now
    await h.crash()
    disarm()

    h.clock.advance(11.0, monotonic=False)  # past the entry deadline, so "never accepted" can be concluded
    h.sim.touch()
    h2 = await h.restart()
    try:
        await h2.until(
            lambda: h2.app.reconciler.recovery_invariant_ok() and not [t for t in h2.engine.open_trades() if t["state"] in ("FLATTENING", "CLOSING")],
            timeout=8, message=f"recovery (kill at call {kill_at}, after={after})",
        )
        signal = h2.signal(signal_id)
        trades = [t for t in h2.trades() if t["signal_id"] == signal_id]
        filled = [t for t in trades if t["filled_size"] > 0]
        entry_cloids = [o["cloid"] for o in h2.orders("ENTRY")]
        entry_fills = [f for f in h2.sim.fills if f.cloid in entry_cloids]
        position = h2.sim.positions.get(COIN, [0.0, 0.0])[0]
        holding = [t for t in trades if t["state"] in ("PROTECTED", "REDUCING")]

        # ---- zero duplicate entries
        assert len(filled) <= 1, f"duplicate entry: {len(filled)} filled trades (kill {kill_at}, after={after})"
        assert len({f.cloid for f in entry_fills}) <= 1 and len(entry_fills) <= 1
        sent = [c for r in h2.sim.requests[baseline:] if r["kind"] == "order" for c in r["detail"] if c in entry_cloids]
        assert len(sent) == len(set(sent)), "an entry request was sent twice"
        # ---- local state equals exchange state
        assert abs(position + sum(t["remaining_size"] for t in holding)) < 1e-9
        assert sum(f.size for f in entry_fills) == pytest.approx(sum(t["filled_size"] for t in filled))
        # ---- never unprotected after recovery
        if abs(position) > 1e-9:
            assert len(holding) == 1
            stops = [o for o in h2.sim.open_orders() if o.is_trigger and o.tpsl == "sl" and o.reduce_only and o.is_buy]
            assert len(stops) == 1 and stops[0].size >= abs(position) - 1e-9 and stops[0].trigger_px == 4296.0
            assert stops[0].cloid in {o["cloid"] for o in h2.orders("SL") if o["state"] == "RESTING"}
            live_tps = [o for o in h2.sim.open_orders() if not o.is_trigger]
            assert sum(o.size for o in live_tps) <= abs(position) + 1e-9  # never more TP size than position
        else:
            assert not holding
            assert not [o for o in h2.sim.open_orders() if o.cloid in {x["cloid"] for x in h2.orders()}]
        assert not [o for o in h2.orders() if o["state"] in ("INTENT", "SUBMITTING", "UNKNOWN")]
        return {"killed": killed, "kill_at": kill_at, "after": after, "signal": signal["state"], "position": position,
                "entries_sent": len(sent)}
    finally:
        await h2.stop()


async def test_tc075_one_hundred_kills_at_random_points_never_duplicate_an_entry_or_leave_a_position_unprotected(tmp_path):
    rng = random.Random(20261011)
    results = [await one_kill(tmp_path, rng, index) for index in range(KILLS)]
    killed = [r for r in results if r["killed"]]
    assert len(killed) >= 80  # most kills really did land inside the sequence
    assert len({(r["kill_at"], r["after"]) for r in killed}) >= 20  # ...at many different points
    outcomes = {r["signal"] for r in results}
    assert "TRADED" in outcomes and outcomes <= {"TRADED", "WAITING_PRICE", "ENTERING", "FAILED"}
    assert any(r["position"] != 0 for r in results) and any(r["position"] == 0 for r in results)
    assert max(r["entries_sent"] for r in results) <= 1
