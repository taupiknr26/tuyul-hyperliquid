"""Scenario outlook end to end (docs/outlook.md §13): the service on
the simulated exchange with canned sources, its HTTP face, and the
page's own script rendering what the service returns. No network."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Optional

import aiohttp
import pytest

from hlbot.core.app import BotApp
from hlbot.domain.outlook import OutlookConfig, build_registry
from hlbot.exchange.base import Candle
from hlbot.ingest.outlook_sources import SourceError, SourceReader
from hlbot.timeutil import ManualClock
from tests.component.conftest import COIN, Harness, make_settings
from tests.component.test_infra import _DOM_STUB

MONDAY = datetime(2026, 10, 12, 6, 46, tzinfo=UTC)  # 13:46 WIB: one minute into the London brief
SPANS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000, "15m": 900_000}
FIVE = 300_000


def ics(*events: tuple[str, datetime]) -> bytes:
    body = "".join(f"BEGIN:VEVENT\r\nDTSTART:{when.strftime('%Y%m%dT%H%M%SZ')}\r\nSUMMARY:{name}\r\nEND:VEVENT\r\n" for name, when in events)
    return f"BEGIN:VCALENDAR\r\n{body}END:VCALENDAR\r\n".encode()


def week(*events: tuple[str, datetime, str]) -> bytes:
    return json.dumps([{"title": name, "country": "USD", "date": when.isoformat(), "impact": impact} for name, when, impact in events]).encode()


class Sources:
    """Canned source documents, by a fragment of the address."""

    def __init__(self, **documents: Any):
        self.documents = documents
        self.calls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        for fragment, document in self.documents.items():
            if fragment in url:
                if isinstance(document, Exception):
                    raise document
                return document
        raise SourceError("HTTP 404")


def fill_market(sim, now_ms: int, price: float = 4200.0) -> None:
    """Candles on every interval the brief reads: a slow wave around ``price``, the last candle still forming."""
    for interval, span in SPANS.items():
        count = 220 if interval in ("4h", "1h") else 40 if interval == "15m" else 16
        first = (now_ms // span - count) * span
        candles = []
        for i in range(count + 1):
            mid = price + 30 * math.sin(i / 5.0) + 12 * math.sin(i / 1.7)
            candles.append(Candle(COIN, interval, first + i * span, first + (i + 1) * span - 1, mid - 1, mid + 4, mid - 4, mid + 1, 10.0, 5))
        sim.candles[(COIN, interval)] = candles


def five(start_ms: int, index: int, low: float, high: float, close: float) -> Candle:
    open_ms = (start_ms // FIVE + 1 + index) * FIVE
    return Candle(COIN, "5m", open_ms, open_ms + FIVE - 1, close, high, low, close, 5.0, 3)


async def start(tmp_path, now: datetime = MONDAY, sources: Optional[Sources] = None, *, enable: tuple[str, ...] = ("ff-calendar",), api: bool = False, **outlook: Any) -> Harness:
    clock = ManualClock(now)
    overrides: dict[str, Any] = {"outlook": OutlookConfig(enabled=True, sources=build_registry(enable=list(enable)), **outlook)}
    if api:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            overrides.update(api_enabled=True, api_port=sock.getsockname()[1])
    h = Harness(tmp_path, clock=clock, **overrides)
    await h.sim.set_quote(COIN, 4200.0, 4200.2, emit=False)
    fill_market(h.sim, clock.now_ms())
    h.settings = make_settings(tmp_path, **overrides)
    h.sources = sources or Sources(bls=ics(("Consumer Price Index", now + timedelta(days=2))), federalreserve=b'{"events": []}')
    h.app = BotApp(h.settings, adapter=h.sim, clock=clock, sender=h._sender, outlook_reader=SourceReader(h.sources),
                   fast_poll_s=0.02, watchdog_s=0.05, market_fresh_for_s=0.2, install_signal_handlers=False)
    await h.app.start()
    return h


@pytest.fixture
async def brief(tmp_path):
    """A bot whose first brief has just been published."""
    created: list[Harness] = []

    async def factory(**kwargs: Any) -> Harness:
        h = await start(tmp_path, **kwargs)
        created.append(h)
        if kwargs.get("published", True) and (h.clock.now() + timedelta(hours=7)).weekday() < 5:
            await h.until(lambda: h.db.scalar("SELECT COUNT(*) FROM outlooks", default=0) or h.db.kv_get("outlook.failure"), message="first brief")
        return h

    yield factory
    for h in created:
        await h.stop()


def latest(h: Harness) -> dict[str, Any]:
    return h.app.outlook.latest()


async def later(h: Harness, **delta: float) -> dict[str, Any]:
    h.clock.advance(timedelta(**delta).total_seconds(), monotonic=False)
    await h.app.outlook.tick()
    return latest(h)


# ---------------------------------------------------------------- generation
async def test_a_brief_is_published_for_the_session_from_several_sources(brief):
    h = await brief(sources=Sources(bls=ics(("Consumer Price Index", MONDAY + timedelta(hours=3))), federalreserve=TimeoutError(),
                                    faireconomy=week(("CPI m/m", MONDAY + timedelta(hours=3), "High"), ("Bank Holiday", MONDAY, "Holiday"))))
    data = latest(h)
    o = data["outlook"]
    assert (o["id"], o["session"], o["trigger"], o["status"], o["generatedAt"], o["validUntil"]) == ("2026-10-12-london", "london", "scheduled", "fresh", "2026-10-12T06:46:00Z", "2026-10-12T13:15:00Z")
    assert data["tz"] == {"name": "WIB", "offsetMinutes": 420} and data["display"] == "XAU/USD" and not data["marketClosed"] and data["nextBrief"]["id"] == "2026-10-12-newyork"
    # one source is down: the brief is built from the rest and says how many answered
    assert (o["sourcesAvailable"], o["sourcesTotal"]) == (6, 7) and not o["flags"]["lowConfidence"] and o["flags"]["thinLiquidity"]
    down = next(s for s in o["sources"] if s["id"] == "fed-calendar")
    assert (down["ok"], down["error"]) == (False, "did not answer in time") and all(s["fetchedAt"] == "2026-10-12T06:46:00Z" for s in o["sources"])
    # every level names its sources and is tagged; the price is the one the trading side already holds
    assert o["price"]["value"] == 4200.1 and 2 <= len([lv for lv in o["levels"] if lv["type"] == "resistance"]) <= 3
    assert all(lv["sources"] and lv["sourceCount"] == len(lv["sources"]) and lv["confluence"] == (lv["sourceCount"] >= 2) for lv in o["levels"])
    assert any(lv["confluence"] for lv in o["levels"])
    assert [s["key"] for s in o["scenarios"]] == ["A", "B", "C"]
    for scenario in (s for s in o["scenarios"] if s["available"]):
        assert scenario["trigger"] and scenario["targets"] and scenario["invalidation"] and scenario["state"] == "watching"
    # the event is confirmed by two sources and carries a no-trade window
    event = o["events"][0]
    assert (event["name"], event["impact"], event["timeConfirmed"], event["sources"]) == ("US CPI", "high", True, ["bls-calendar", "ff-calendar"])
    assert event["noTradeWindowMin"] == {"before": 15, "after": 15} and o["nextHighImpact"]["key"] == "cpi"
    assert o["drivers"] and all(d["sources"] for d in o["drivers"]) and o["label"] == "Scenario outlook, not news" and "Not financial advice" in o["disclaimer"]
    assert h.audit("outlook.published")[0]["payload"] and h.sim.requests == [] or all(r["kind"] != "order" for r in h.sim.requests)  # it never sends an order
    # the same pass does not publish a second brief for the same session
    await later(h, minutes=10)
    assert h.db.scalar("SELECT COUNT(*) FROM outlooks") == 1


async def test_a_late_start_publishes_at_once_and_a_single_level_source_is_low_confidence(brief):
    h = await brief(now=MONDAY + timedelta(hours=2), enable=(), sources=Sources(), min_level_sources=5)
    o = latest(h)["outlook"]
    assert o["trigger"] == "catchup" and o["flags"]["lowConfidence"] and o["levelSourcesAvailable"] == 4 and (o["sourcesAvailable"], o["sourcesTotal"]) == (4, 6)
    assert o["events"] == [] and o["nextHighImpact"] is None and o["drivers"] == []  # the calendars did not answer: unavailable, not invented


async def test_nothing_is_published_when_no_source_answers_and_the_last_brief_stays(brief):
    h = await brief()
    first = latest(h)["outlook"]["id"]
    h.sim.faults.rest_down = True
    h.sources.documents = {}
    data = await later(h, hours=7)  # the New York brief is due
    assert data["outlook"]["id"] == first and data["outlook"]["status"] == "stale" and data["outlook"]["stale"]["kind"] == "expired"
    assert data["failure"]["problems"] == ["no price is available from the market feed"] or data["failure"]["sourcesAvailable"] == 0
    assert h.db.scalar("SELECT COUNT(*) FROM outlooks") == 1 and h.audit("outlook.failed")
    h.sim.faults.rest_down = False
    data = await later(h, minutes=3)  # tried again a little later
    assert data["outlook"]["id"] == "2026-10-12-newyork" and data["failure"] is None


async def test_no_brief_at_the_weekend_and_the_week_is_recapped(brief):
    h = await brief(now=datetime(2026, 10, 17, 4, 0, tzinfo=UTC))  # Saturday 11:00 WIB
    await h.app.outlook.tick()
    data = latest(h)
    assert data["marketClosed"] and data["outlook"] is None and data["nextBrief"]["id"] == "2026-10-19-asia" and data["nextBrief"]["at"] == "2026-10-18T23:45:00Z"
    assert data["recap"]["generated"]["total"] == 0 and h.db.scalar("SELECT COUNT(*) FROM outlooks") == 0
    assert (await h.app.outlook.generate("manual"))["ok"] is False


# ---------------------------------------------------------------- states, outcomes, early invalidation
async def test_scenarios_are_followed_and_their_outcomes_settled_from_the_price_feed(brief):
    h = await brief()
    o = latest(h)["outlook"]
    a, b, c = o["scenarios"]
    level, target, start_ms = b["triggerRule"]["price"], b["targets"][0], h.clock.now_ms()
    h.sim.candles[(COIN, "5m")] = [five(start_ms, 0, level - 2, level + 1.2, level + 1), five(start_ms, 1, level + 0.5, level + 3, level + 2)]
    o = (await later(h, minutes=12))["outlook"]
    states = {s["key"]: (s["state"], s["outcome"]) for s in o["scenarios"]}
    assert states["B"] == ("triggered", None) and states["C"] == ("watching", None) and o["scenarios"][1]["triggeredAt"]
    assert states["A"][0] in ("invalidated", "watching")  # a rejection at the same level fails when price closes through it
    h.sim.candles[(COIN, "5m")].append(five(start_ms, 2, level + 1, target + 1, target))
    o = (await later(h, minutes=6))["outlook"]
    assert {s["key"]: s["state"] for s in o["scenarios"]}["B"] == "target" and not o["final"]
    data = await later(h, hours=6, minutes=30)  # past the end of the brief: settled, and the next session's brief is out
    assert data["outlook"]["id"] == "2026-10-12-newyork"
    settled = h.app.outlook.get("2026-10-12-london")
    assert settled["final"] and settled["status"] == "stale" and {s["key"]: s["outcome"] for s in settled["scenarios"]}["B"] == "target"
    assert {s["key"]: s["outcome"] for s in settled["scenarios"]}["C"] == "not_triggered"
    history = h.app.outlook.history("local", None, None, None)
    assert [item["id"] for item in history["items"]] == ["2026-10-12-newyork", "2026-10-12-london"]
    assert [s["outcome"] for s in history["items"][1]["scenarios"] if s["key"] == "B"] == ["target"]
    assert h.app.outlook.history("local", "2026-10-12", "2026-10-12", "london")["items"][0]["id"] == "2026-10-12-london"
    assert h.app.outlook.history("local", "2026-10-13", None, None)["items"] == []
    summary = h.app.outlook.summary("local", None, None)
    by_scenario = {row["key"]: row for row in summary["generated"]["byScenario"]}
    assert by_scenario["B"]["target"] == 1 and by_scenario["C"]["not_triggered"] == 1 and summary["generated"]["byWeek"][0]["key"] == "2026-10-12"
    assert summary["caption"] == "Past outcomes do not predict future results" and summary["mine"]["total"] == 0


async def test_both_major_levels_breaking_makes_the_brief_stale(brief):
    h = await brief()
    _, b, c = latest(h)["outlook"]["scenarios"]
    up, down, start_ms = b["triggerRule"]["price"], c["triggerRule"]["price"], h.clock.now_ms()
    h.sim.candles[(COIN, "5m")] = [five(start_ms, 0, up - 1, up + 1.5, up + 1), five(start_ms, 1, down - 1.5, up, down - 1)]
    o = (await later(h, minutes=16))["outlook"]  # both candles have closed
    assert o["status"] == "stale" and o["stale"]["kind"] == "levels" and "Both major levels broke" in o["stale"]["reason"] and not o["editable"]
    assert h.db.scalar("SELECT COUNT(*) FROM outlooks") == 1  # marked, not refreshed by itself


async def test_a_high_impact_release_makes_the_brief_stale_and_a_refreshed_one_follows(brief):
    release = MONDAY + timedelta(minutes=30)
    h = await brief(sources=Sources(bls=ics(("Consumer Price Index", release)), federalreserve=b'{"events": []}'))
    assert latest(h)["outlook"]["events"][0]["timeConfirmed"] is False  # one source: shown, flagged
    assert (await later(h, minutes=32))["outlook"]["id"] == "2026-10-12-london"  # not yet: the refresh waits out the first minutes
    data = await later(h, minutes=4)
    fresh, old = data["outlook"], h.app.outlook.get("2026-10-12-london")
    assert (fresh["id"], fresh["trigger"], fresh["status"]) == ("2026-10-12-london-2", "event", "fresh") and fresh["validUntil"] == old["validUntil"]
    assert old["status"] == "stale" and old["stale"]["kind"] == "event" and old["stale"]["reason"] == "High-impact release: US CPI."
    assert (await later(h, minutes=10))["outlook"]["id"] == "2026-10-12-london-2"  # once, not on every pass


# ---------------------------------------------------------------- the HTTP face and My Version
async def test_outlook_api_roles_my_version_and_reset(brief):
    h = await brief(api=True)
    base = f"http://127.0.0.1:{h.settings.api_port}"
    token = {"X-Control-Token": h.settings.control_token_path.read_text()}
    async with aiohttp.ClientSession() as anon, aiohttp.ClientSession(headers=token) as http:
        for path in ("/api/outlook/latest", "/api/outlook/history", "/api/outlook/history/summary", "/api/outlook/2026-10-12-london", "/api/outlook/2026-10-12-london/mine"):
            async with anon.get(base + path) as response:
                assert response.status == 401, path
        async with http.get(base + "/api/outlook/latest", params={"instrument": "XAUUSD"}) as response:
            o = (await response.json())["outlook"]
            assert response.status == 200 and o["id"] == "2026-10-12-london"
        async with http.get(base + "/api/outlook/latest", params={"instrument": "EURUSD"}) as response:
            assert response.status == 404
        async with http.get(base + "/api/outlook/nope") as response:
            assert response.status == 404 and (await response.json())["error"] == "no such brief"
        mine = f"{base}/api/outlook/{o['id']}/mine"
        async with http.get(mine) as response:
            assert await response.json() == {"outlookId": o["id"], "editable": True, "customLevels": [], "archivedLevels": [], "scenarioEdits": [], "scenarios": {}, "journalNote": "", "carryDistance": 50.0}

        # custom levels: saved for this user, shown as theirs
        async with http.put(mine, json={"customLevels": [{"price": 4190, "type": "support", "note": "Yesterday's low"}, {"price": 4240, "type": "resistance"}]}) as response:
            saved = await response.json()
            assert response.status == 200 and [(lv["price"], lv["type"], lv["note"]) for lv in saved["customLevels"]] == [(4240.0, "resistance", ""), (4190.0, "support", "Yesterday's low")]
        # an invalid edit names each fault and saves nothing at all, the valid level beside it included
        a = o["scenarios"][0]
        short = a["direction"] == "short"
        bad = {"scenarioEdits": [{"key": "A", "stop": a["entryZone"][0] - 50 if short else a["entryZone"][1] + 50}], "customLevels": [], "journalNote": "should not be saved"}
        async with http.put(mine, json=bad) as response:
            refused = await response.json()
            assert response.status == 400 and refused["ok"] is False and "scenarioEdits.A.stop" in refused["errors"] and "the stop must be" in refused["errors"]["scenarioEdits.A.stop"]
        async with http.get(mine) as response:
            kept = await response.json()
            assert len(kept["customLevels"]) == 2 and kept["journalNote"] == "" and kept["scenarios"] == {}
        for body in ({"scenarioEdits": [{"key": "Z"}]}, {"customLevels": [{"price": -1, "type": "support"}]}, {"journalNote": "x" * 2001}, {"colour": "red"}, {}):
            async with http.put(mine, json=body) as response:
                assert response.status == 400, body
        # a valid edit becomes My Version; the generated scenario stays as it was
        stop = a["stop"] + 1 if short else a["stop"] - 1
        async with http.put(mine, json={"scenarioEdits": [{"key": "A", "trigger": "My own trigger", "stop": stop}], "journalNote": "Took A only."}) as response:
            saved = await response.json()
            assert response.status == 200 and saved["scenarios"]["A"]["edited"] and (saved["scenarios"]["A"]["stop"], saved["scenarios"]["A"]["trigger"]) == (stop, "My own trigger")
            assert saved["journalNote"] == "Took A only." and saved["scenarioEdits"][0]["editedAt"] == "2026-10-12T06:46:00Z"
        async with http.get(f"{base}/api/outlook/{o['id']}") as response:
            assert (await response.json())["scenarios"][0]["stop"] == a["stop"]
        # the edited scenario is followed apart from the generated one
        await later(h, minutes=12)
        assert h.app.outlook.history("local", None, None, None)["items"][0]["mine"] == [{"key": "A", "state": "watching", "outcome": None}]
        async with http.delete(f"{mine}/scenarios/A") as response:
            assert (await response.json())["scenarios"] == {}
        # a refresh is an operator's to ask for, and not twice in a row
        async with http.post(base + "/api/outlook/refresh") as response:
            assert response.status == 200 and (await response.json())["id"] == "2026-10-12-london-2"
        async with http.post(base + "/api/outlook/refresh") as response:
            assert response.status == 429 and "try again in" in (await response.json())["error"]
        # the replaced brief keeps the note but takes no more edits; the levels carried over to the new one
        async with http.put(mine, json={"scenarioEdits": [{"key": "B"}]}) as response:
            assert response.status == 400 and "no longer current" in (await response.json())["errors"]["scenarioEdits"]
        async with http.put(mine, json={"journalNote": "Later thought."}) as response:
            assert response.status == 200
        async with http.get(f"{base}/api/outlook/2026-10-12-london-2/mine") as response:
            carried = await response.json()
            assert len(carried["customLevels"]) == 2 and carried["scenarios"] == {} and carried["journalNote"] == ""

        # a viewer reads and keeps a layer of their own, but cannot ask for a refresh
        async with http.post(base + "/api/users", json={"username": "vera", "password": "viewer-password-1", "role": "viewer"}) as response:
            assert response.status == 200
        page = {"X-Requested-With": "hlbot"}
        async with aiohttp.ClientSession(headers=page, cookie_jar=aiohttp.CookieJar(unsafe=True)) as viewer:  # the jar must keep a cookie from an IP address
            async with viewer.post(base + "/api/login", json={"username": "vera", "password": "viewer-password-1"}) as response:
                assert response.status == 200
            async with viewer.post(base + "/api/outlook/refresh") as response:
                assert response.status == 403
            theirs = f"{base}/api/outlook/2026-10-12-london-2/mine"
            async with viewer.get(theirs) as response:
                assert (await response.json())["customLevels"] == []  # another user's levels are not theirs
            async with viewer.put(theirs, json={"customLevels": [{"price": 4000, "type": "support"}]}) as response:
                assert response.status == 200
            async with viewer.put(theirs, json={"customLevels": []}, headers={"X-Requested-With": "elsewhere"}) as response:
                assert response.status == 403  # a write that did not come from the dashboard page
        async with http.delete(base + "/api/users/vera") as response:
            assert response.status == 200
        assert h.db.scalar("SELECT COUNT(*) FROM outlook_user_levels WHERE username = 'vera'") == 0


async def test_a_users_level_left_far_behind_is_archived_at_the_next_brief(brief):
    h = await brief()
    service = h.app.outlook
    saved = service.save_mine("local", "2026-10-12-london", {"customLevels": [{"price": 4190, "type": "support"}, {"price": 4400, "type": "resistance", "note": "far"}]})
    assert len(saved["customLevels"]) == 2
    await later(h, hours=7)  # the New York brief: 4400 is more than 50 from price
    mine = service.mine("local", "2026-10-12-newyork")
    assert [lv["price"] for lv in mine["customLevels"]] == [4190.0]
    assert [(lv["price"], lv["note"], lv["archivedBy"]) for lv in mine["archivedLevels"]] == [(4400.0, "far", "2026-10-12-newyork")]
    assert service.save_mine("local", "2026-10-12-newyork", {"clearArchived": True})["archivedLevels"] == []


async def test_switched_off_serves_nothing_and_starts_nothing(make):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    h = await make(api_enabled=True, api_port=port)  # a Settings built in code: the outlook is off
    assert h.app.outlook is None and all(task.get_name() != "outlook" for task in h.app.tasks)
    async with aiohttp.ClientSession(headers={"X-Control-Token": h.settings.control_token_path.read_text()}) as http:
        async with http.get(f"http://127.0.0.1:{port}/api/outlook/latest") as response:
            assert response.status == 200 and (await response.json())["enabled"] is False
        async with http.get(f"http://127.0.0.1:{port}/api/outlook/history") as response:
            assert response.status == 404


# ---------------------------------------------------------------- the page
_OUTLOOK_TAIL = r"""
const api = new Function(script + "\nreturn {render, renderOutlook, renderOutlookHistory, olScenarios, set(d, mine, version, editing, errors) { olData = d; olMine = mine || null; olVersion = version || 'generated'; olEditing = editing || null; olErrors = errors || {}; olDraft = null; }};")();
const input = JSON.parse(fs.readFileSync(statusPath, "utf8"));
Date.now = () => input.nowMs;
const grab = () => { const out = {}; for (const [id, e] of Object.entries(ids)) if (id.startsWith("ol-") || id.startsWith("oh-") || id === "outlook-sub") out[id] = {html: e._html, text: e.textContent, cls: e.className, hidden: e.hidden}; return out; };
const out = {};
api.render(input.status);
api.set(input.latest, input.mine, "generated"); api.renderOutlook(); out.generated = grab();
api.set(input.latest, input.mine, "mine"); api.renderOutlook(); out.mine = grab();
api.set(input.latest, input.mine, "mine", "A", {stop: "the stop must be above the entry zone for a short"}); api.olScenarios(input.latest.outlook); out.form = ids["ol-scenarios"]._html;
api.renderOutlookHistory(input.history, input.summary); out.history = grab();
Date.now = () => input.nowMs + 9 * 3600 * 1000;
api.set(input.latest, input.mine, "mine"); api.renderOutlook(); out.expired = grab();
Date.now = () => input.nowMs;
api.set({...input.latest, outlook: null, marketClosed: true, failure: {sourcesAvailable: 0, sourcesTotal: 6, problems: ["no source answered"]}, recap: input.summary}); api.renderOutlook(); out.closed = grab();
api.set({enabled: false, reason: "off"}); api.renderOutlook(); out.off = grab();
process.stdout.write(JSON.stringify(out));
"""


async def test_the_page_renders_a_brief_and_escapes_what_it_shows(brief, tmp_path):
    """The page's own script under Node, fed what the service returns
    with hostile text planted wherever a source or a user supplies it.
    Skipped where Node is not installed."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    h = await brief(sources=Sources(bls=ics(("Consumer Price Index", MONDAY + timedelta(hours=3))), federalreserve=b'{"events": []}'))
    service = h.app.outlook
    b = latest(h)["outlook"]["scenarios"][1]
    service.save_mine("local", "2026-10-12-london", {"customLevels": [{"price": 4188, "type": "support", "note": "<img src=x onerror=alert(1)>"}],
                                                      "scenarioEdits": [{"key": "B", "trigger": "<script>alert(2)</script>"}], "journalNote": "</textarea><b>note</b>"})
    h.sim.candles[(COIN, "5m")] = [five(h.clock.now_ms(), 0, b["triggerRule"]["price"] - 1, b["triggerRule"]["price"] + 1.2, b["triggerRule"]["price"] + 1)]
    data = await later(h, minutes=12)
    o = data["outlook"]
    o["sources"][0].update(title="<i>Pivots</i>", url="javascript:alert(3)")
    o["levels"][0]["role"] = "<u>role</u>"
    o["bias"]["reason"] = "<b>reason</b>"
    o["drivers"].append({"label": "<i>Oil</i>", "text": "<a href=x>headline</a>", "sources": ["hl-pivots"], "url": "javascript:alert(4)"})
    o["events"][0]["name"] = "<em>CPI</em>"
    o["scenarios"][2] = {"key": "C", "name": "Breakdown", "available": False, "reason": "<s>none</s>", "state": None, "outcome": None}
    payload = {
        "nowMs": h.clock.now_ms(), "latest": data, "mine": service.mine("local", o["id"]), "status": {"health": {"state": "HEALTHY", "control": {"mode": "RUNNING"}, "conditions": []},
        "live": {"quotes": {COIN: {"bid": 4201.0, "ask": 4201.4, "mark": 4201.2}}}}, "history": service.history("local", None, None, None), "summary": service.summary("local", None, None),
    }
    payload["summary"]["generated"] = {"total": 3, "bySession": [{"key": "london", "not_triggered": 1, "target": 1, "invalidation": 1, "expired_open": 0, "total": 3}],
                                       "byScenario": [{"key": "A", "not_triggered": 0, "target": 1, "invalidation": 0, "expired_open": 0, "total": 1}], "byWeek": [{"key": "2026-10-12", "not_triggered": 1, "target": 1, "invalidation": 1, "expired_open": 0, "total": 3}]}
    page, stub, data_file = tmp_path / "dashboard.html", tmp_path / "stub.js", tmp_path / "outlook.json"
    page.write_text(resources.files("hlbot.api").joinpath("dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")
    stub.write_text(_DOM_STUB.split("const api = new Function(")[0] + _OUTLOOK_TAIL)
    data_file.write_text(json.dumps(payload, default=str))
    done = await asyncio.to_thread(subprocess.run, ["node", str(stub), str(page), str(data_file)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]
    out = json.loads(done.stdout)

    g = out["generated"]
    # the markup that reaches the document (the stub's "text" is that markup with its tags and entities resolved)
    everything = out["form"] + "".join(e["html"] for view in ("generated", "mine", "history", "expired", "closed", "off") for e in out[view].values())
    for planted in ("<img src=x", "<script>alert(2)", "<i>Pivots</i>", "<u>role</u>", "<b>reason</b>", "<a href=x>", "<em>CPI</em>", "<s>none</s>", "<i>Oil</i>", "</textarea><b>"):
        assert planted not in everything, planted
    assert "javascript:" not in everything  # a link that is not https is shown as text, never made clickable
    assert "&lt;u&gt;role&lt;/u&gt;" in g["ol-levels"]["html"] and "&lt;em&gt;CPI&lt;/em&gt;" in g["ol-events"]["html"] and "&lt;s&gt;none&lt;/s&gt;" in g["ol-scenarios"]["html"]
    # header: title, session, status, validity, the label; all times in WIB
    assert g["ol-title"]["text"] == "Scenario Outlook: XAU/USD" and g["ol-session"]["text"] == "London" and g["ol-status"]["text"] == "Fresh" and g["ol-label"]["text"] == "Scenario outlook, not news"
    assert "Generated Mon 12 Oct 13:46 WIB · valid until Mon 12 Oct 20:15 WIB" in g["ol-generated"]["text"] and "valid for 6 h 17 m" in g["ol-countdown"]["text"]
    assert g["ol-root"]["hidden"] is False and g["outlook-sub"]["text"] == "London · fresh"
    # context: the live price from the status feed, the bias and its sources
    assert g["ol-price"]["text"].startswith("4,201.2") and "since the brief" in g["ol-price"]["text"] and "Top drivers" in g["ol-context"]["text"]
    # levels: source count, confluence or single-source tag, distance from the live price, the nearest one marked
    levels = g["ol-levels"]["html"]
    assert "Confluence" in levels and "Single source" in levels and levels.count('class="nearest"') == 1 and "From price" in levels and "Mine" not in levels
    # scenarios: the triggered one is highlighted, the unavailable one says so
    cards = g["ol-scenarios"]["html"]
    assert 'class="ol-card active"' in cards and "Active" in cards and "Unavailable" in cards and "Invalidation" in cards and "Risk : reward" in cards and "data-ol=\"edit\"" not in cards
    assert "No-trade window" in g["ol-events"]["text"] and 'class="win"' in g["ol-events"]["html"] and "Time unconfirmed" in g["ol-events"]["text"]
    assert g["ol-next-event"]["text"] == "next high-impact: US CPI in 2 h 48 m"
    assert g["ol-sources-count"]["text"] == "6 of 7 sources available" and "Not financial advice" in g["ol-disclaimer"]["text"] and "Position sizing" in g["ol-notes"]["text"]
    # My Version: the user's level and edit on top, with the controls to change them
    m = out["mine"]
    assert "Mine" in m["ol-levels"]["html"] and "&lt;img src=x onerror=alert(1)&gt;" in m["ol-levels"]["html"] and 'data-ol="drop-level"' in m["ol-levels"]["html"]
    assert "Edited" in m["ol-scenarios"]["html"] and "&lt;script&gt;alert(2)&lt;/script&gt;" in m["ol-scenarios"]["html"] and 'data-ol="reset" data-key="B"' in m["ol-scenarios"]["html"]
    assert "Add my level" in m["ol-levels-mine"]["html"] and "within 50 of price" in m["ol-levels-mine"]["text"]
    assert 'data-ol-form="A"' in out["form"] and "the stop must be above the entry zone for a short" in out["form"]
    # history: outcomes per scenario, the summary tables and the caption
    hist = out["history"]
    assert "By session" in hist["oh-summary"]["html"] and "Triggered, hit target" in hist["oh-summary"]["html"] and "week of 2026-10-12" in hist["oh-summary"]["text"]
    assert hist["oh-count"]["text"] == "1 brief" and "B open" in hist["oh-list"]["html"] and "My edited versions" in hist["oh-list"]["html"] and "&lt;/textarea&gt;&lt;b&gt;note&lt;/b&gt;" in hist["oh-list"]["html"]
    # expired: labelled, dimmed, no longer editable
    e = out["expired"]
    assert e["ol-status"]["text"] == "Stale" and "Outlook expired" in e["ol-countdown"]["text"] and "Outlook expired" in e["ol-banners"]["text"]
    assert 'data-ol="edit"' not in e["ol-scenarios"]["html"] and "cannot be edited" in e["ol-scenarios"]["html"]
    # weekend with nothing published, a failed brief, and the module switched off
    closed = out["closed"]
    assert closed["ol-root"]["hidden"] is True and "Market closed for the weekend" in closed["ol-banners"]["text"] and "3 scenarios settled" in closed["ol-banners"]["text"]
    assert "could not be published (0 of 6 sources answered)" in closed["ol-banners"]["text"] and "Past outcomes do not predict future results" in closed["ol-banners"]["text"]
    assert "switched off" in out["off"]["ol-state"]["text"] and out["off"]["ol-root"]["hidden"] is True
    # no push notification code or setting exists (§13)
    html = page.read_text(encoding="utf-8")
    assert "Past outcomes do not predict future results" in html and "Scenario outlook, not news" in html and "Not financial advice" in html
    assert "Notification" not in html.split("<script>")[-1].replace("Notifications</dt>", "") and "serviceWorker" not in html and "PushManager" not in html
