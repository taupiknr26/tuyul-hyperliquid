"""Scenario outlook: the pure rules, the source parsers and the settings
(docs/outlook.md §4, §6, §8, §10, §11)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from hlbot.config import ConfigError, load_settings
from hlbot.domain import outlook as o
from hlbot.domain.outlook import Headline, Lean, RawEvent, RawLevel, Reading
from hlbot.ingest import outlook_sources as src

CFG = o.OutlookConfig(enabled=True)
MONDAY = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)  # 14:00 WIB: inside the London brief


@dataclass
class Bar:
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float


def bar(start_ms: int, low: float, high: float, close: float, minutes: int = 5) -> Bar:
    return Bar(start_ms, start_ms + minutes * 60_000 - 1, close, high, low, close)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ---------------------------------------------------------------- schedule (§4)
def test_three_briefs_a_weekday_each_valid_until_the_next():
    asia, london, newyork = o.current_slot(CFG, at("2026-10-12T00:00")), o.current_slot(CFG, MONDAY), o.current_slot(CFG, at("2026-10-12T14:00"))
    assert (asia.id, london.id, newyork.id) == ("2026-10-12-asia", "2026-10-12-london", "2026-10-12-newyork")
    assert asia.start == at("2026-10-11T23:45") and asia.end == london.start == at("2026-10-12T06:45")  # 06:45 and 13:45 WIB
    assert newyork.start == at("2026-10-12T13:15") and newyork.opens == at("2026-10-12T13:30") and newyork.end == at("2026-10-12T23:45")
    # the New York brief still covers the small hours of the next local day
    assert o.current_slot(CFG, at("2026-10-12T22:00")).id == "2026-10-12-newyork"


def test_no_brief_between_fridays_last_and_mondays_first():
    assert o.current_slot(CFG, at("2026-10-16T22:00")).id == "2026-10-16-newyork"  # Saturday 05:00 WIB: Friday's brief still runs
    assert o.current_slot(CFG, at("2026-10-17T03:00")) is None and o.current_slot(CFG, at("2026-10-18T12:00")) is None
    assert o.next_slot(CFG, at("2026-10-17T03:00")).id == "2026-10-19-asia"
    assert o.next_slot(CFG, MONDAY).id == "2026-10-12-newyork"


def test_session_times_are_configuration_and_are_checked():
    winter = o.OutlookConfig(sessions=o.check_sessions({"asia": ["06:45", "07:00"], "london": ["14:45", "15:00"], "newyork": ["21:15", "21:30"]}))
    assert o.current_slot(winter, at("2026-12-01T08:00")).start == at("2026-12-01T07:45")
    for bad in ({"asia": ["06:45", "07:00"]}, {**o.DEFAULT_SESSIONS, "london": ["25:00", "26:00"]}, {**o.DEFAULT_SESSIONS, "london": ["05:00", "05:10"]},
                {**o.DEFAULT_SESSIONS, "asia": ["06:45", "06:00"]}, {**o.DEFAULT_SESSIONS, "asia": "06:45"}):
        with pytest.raises(ValueError):
            o.check_sessions(bad)


def test_status_follows_the_validity_and_times_read_in_the_outlook_zone():
    start, end = at("2026-10-12T06:45"), at("2026-10-12T13:15")
    assert o.status_of(start, end, None, at("2026-10-12T07:00")) == "fresh"
    assert o.status_of(start, end, None, at("2026-10-12T11:40")) == "aging"  # past 75 % of the validity
    assert o.status_of(start, end, None, end) == "stale"
    assert o.status_of(start, end, at("2026-10-12T07:30"), at("2026-10-12T07:31")) == "stale"  # invalidated early
    assert o.local_text(at("2026-10-14T12:30"), CFG) == "Wed 14 Oct 19:30 WIB"
    assert o.eastern_to_utc(datetime(2026, 10, 14, 8, 30)) == at("2026-10-14T12:30")  # daylight time
    assert o.eastern_to_utc(datetime(2026, 12, 9, 14, 0)) == at("2026-12-09T19:00")   # standard time
    assert o.eastern_to_utc(datetime(2026, 3, 8, 3, 0)) == at("2026-03-08T07:00") and o.eastern_to_utc(datetime(2026, 11, 1, 3, 0)) == at("2026-11-01T08:00")


# ---------------------------------------------------------------- cross-checking (§8.3)
def test_levels_within_tolerance_merge_into_a_weighted_confluence():
    raw = {"a": [RawLevel(4220.0, "Pivot R1", "1D"), RawLevel(4100.0, "Pivot S1", "1D")], "b": [RawLevel(4223.0, "Swing high", "4H")], "c": [RawLevel(4226.0, "50 SMA", "1H")]}
    clusters = o.cluster_levels(raw, {"a": 1.0, "b": 2.0, "c": 1.0}, 5.0)
    assert [(c["price"], c["sources"], c["confluence"]) for c in clusters] == [(4100.0, ["a"], False), (4222.0, ["a", "b"], True), (4226.0, ["c"], False)]
    assert clusters[1]["role"] == "Pivot R1 (1D) + Swing high (4H)" and clusters[1]["timeframe"] == "1D/4H"
    # the cluster is measured from its first member, so 4220, 4223, 4226 do not chain into one level
    levels = o.select_levels(clusters, 4200.0, min_gap=1.0)
    assert [(lv["id"], lv["price"], lv["type"], lv["sourceCount"], lv["confluence"], lv["singleSource"]) for lv in levels] == [
        ("lvl-1", 4226.0, "resistance", 1, False, True), ("lvl-2", 4222.0, "resistance", 2, True, False), ("lvl-3", 4100.0, "support", 1, False, True)]


def test_a_confluence_outranks_a_nearer_single_source_level_and_price_itself_is_no_level():
    clusters = [{"price": 4200.4, "role": "x", "timeframe": "1H", "sources": ["a"], "weight": 1.0, "confluence": False}]
    clusters += [{"price": 4200.0 + step, "role": "r", "timeframe": "1H", "sources": ["a"], "weight": 1.0, "confluence": False} for step in (5, 10, 15)]
    clusters += [{"price": 4220.0, "role": "c", "timeframe": "4H", "sources": ["a", "b"], "weight": 2.0, "confluence": True}]
    chosen = [lv["price"] for lv in o.select_levels(clusters, 4200.0, min_gap=2.0)]
    assert chosen == [4220.0, 4210.0, 4205.0]  # 4200.4 is the price; the confluence at 4220 displaces the single-source 4215


def test_opposite_leans_give_a_mixed_bias_with_both_views():
    weights = {"a": 1.0, "b": 1.0, "c": 1.0}
    mixed = o.reconcile_bias({"a": Lean("bullish", "up"), "b": Lean("bearish", "down"), "c": Lean("neutral", "flat")}, weights)
    assert mixed["direction"] == "mixed" and mixed["agreement"] == "mixed" and "1 lean bullish, 1 lean bearish" in mixed["reason"]
    assert [(v["direction"], v["sources"]) for v in mixed["views"]] == [("bullish", ["a"]), ("bearish", ["b"]), ("neutral", ["c"])]
    assert o.reconcile_bias({"a": Lean("bullish", "up"), "c": Lean("neutral", "flat")}, weights)["agreement"] == "partial"
    aligned = o.reconcile_bias({"a": Lean("bearish", "down"), "b": Lean("bearish", "lower")}, {"a": 1.0, "b": 3.0})
    assert (aligned["direction"], aligned["agreement"], aligned["reason"], aligned["sources"]) == ("bearish", "aligned", "lower", ["b", "a"])
    assert o.reconcile_bias({}, weights)["direction"] is None  # nothing to go on: unavailable, not a guess


def test_event_times_are_confirmed_only_when_two_sources_agree():
    cpi, speech = at("2026-10-12T12:30"), at("2026-10-12T18:00")
    raw = {
        "bls": [RawEvent("Consumer Price Index", cpi, None, "USD"), RawEvent("Real Earnings", cpi, None, "USD")],
        "ff": [RawEvent("Core CPI m/m", cpi, "high", "USD"), RawEvent("CPI y/y", cpi, "high", "USD"), RawEvent("Fed Chairman Warsh Speaks", speech, "high", "USD"),
               RawEvent("ADP Non-Farm Employment Change", cpi, "high", "USD"), RawEvent("Unemployment Claims", cpi, "medium", "USD"),
               RawEvent("FOMC Member Barkin Speaks", cpi, "low", "USD"), RawEvent("Bank Holiday", at("2026-10-12T11:00"), "holiday", "USD")],
        "fed": [RawEvent("Discussion - Chairman Kevin Warsh", speech - timedelta(minutes=30), None, "USD"), RawEvent("Speech - Vice Chair Jefferson", speech, None, "USD")],
    }
    calendar = o.reconcile_events(raw, CFG, MONDAY)
    by_key = {e["key"]: e for e in calendar["timeline"]}
    assert by_key["cpi"]["timeConfirmed"] and by_key["cpi"]["sources"] == ["bls", "ff"] and by_key["cpi"]["noTradeWindowMin"] == {"before": 15, "after": 15}
    chair = by_key["fed_chair"]  # the sources differ: the earlier time, flagged
    assert chair["timeUtc"] == "2026-10-12T17:30:00Z" and not chair["timeConfirmed"] and "different times" in chair["timeNote"]
    assert by_key["unemployment-claims"]["impact"] == "medium" and by_key["unemployment-claims"]["noTradeWindowMin"] is None
    assert by_key["adp-non-farm-employment-change"]["name"].startswith("ADP")  # not mistaken for the jobs report
    assert "nfp" not in by_key and len(by_key) == 4  # earnings, low-impact speakers and the vice chair are left out
    assert calendar["holidays"] == ["Bank Holiday"] and calendar["nextHigh"]["key"] == "cpi"
    single = o.reconcile_events({"bls": raw["bls"]}, CFG, MONDAY)["timeline"][0]
    assert not single["timeConfirmed"] and single["timeNote"] == "one source only"


def test_drivers_are_traceable_and_one_headline_serves_one_driver():
    upcoming = o.reconcile_events({"fed": [RawEvent("FOMC Meeting", at("2026-10-28T18:00"), None, "USD")], "bls": [RawEvent("Producer Price Index", at("2026-10-15T12:30"), None, "USD")]}, CFG, MONDAY)["upcoming"]
    news = {"wire": [Headline("Gold falls as dollar firms, Mideast truce weighed", "https://example.com/a", MONDAY), Headline("Oil dips on OPEC supply", "https://example.com/b", MONDAY - timedelta(hours=1))]}
    drivers = o.build_drivers(upcoming, news, CFG)
    assert [d["label"] for d in drivers] == ["Fed", "USD/yields", "Oil"] and all(d["sources"] for d in drivers)
    assert drivers[0]["text"] == "FOMC rate decision is due Thu 29 Oct 01:00 WIB (time unconfirmed)." and drivers[0]["sources"] == ["fed"]
    assert drivers[1]["url"] == "https://example.com/a" and drivers[2]["url"] == "https://example.com/b"
    assert [d["label"] for d in o.build_drivers(upcoming, {}, CFG)] == ["Fed", "US data"] and o.build_drivers([], {}, CFG) == []


# ---------------------------------------------------------------- scenarios and the gates (§5.4, §11)
def levels_around(price: float = 4200.0) -> list[dict]:
    clusters = o.cluster_levels({"a": [RawLevel(price + d, "L", "1D") for d in (-40, -20, 15, 30, 55)], "b": [RawLevel(price + 15.5, "M", "4H")]}, {"a": 1.0, "b": 1.0}, 5.0)
    return o.select_levels(clusters, price, min_gap=1.0)


def test_three_scenarios_with_trigger_target_and_invalidation_on_the_right_side():
    a, b, c = o.build_scenarios(levels_around(), 4200.0, "bearish", atr_15m=4.0)
    assert (a["name"], a["direction"], a["entryZone"], a["stop"], a["targets"]) == ("Rejection", "short", [4211.2, 4215.2], 4217.2, [4180.0, 4160.0])
    assert a["invalidation"] == "5m close above 4,215.2" and a["triggerRule"] == {"type": "touch", "price": 4211.2, "timeframe": "5m"} and a["retest"] is None
    assert (b["name"], b["direction"], b["trigger"], b["entryZone"], b["stop"], b["targets"]) == ("Breakout", "long", "5m close above 4,215.2", [4215.2, 4219.2], 4213.2, [4230.0, 4255.0])
    assert (c["name"], c["direction"], c["trigger"], c["stop"], c["targets"]) == ("Breakdown", "short", "5m close below 4,180", 4182.0, [4160.0]) and "old support as resistance" in c["retest"]
    assert a["riskReward"] == pytest.approx((4213.2 - 4180.0) / 4.0, abs=0.01) and all(not o.scenario_problems(s) for s in (a, b, c))
    assert set(a["sources"]) == {"a", "b"} and a["state"] == "watching" and a["outcome"] is None
    # the rejection follows the bias: bullish fades support instead; with no bias it takes the nearer level
    assert o.build_scenarios(levels_around(), 4200.0, "bullish", 4.0)[0]["direction"] == "long"
    assert o.build_scenarios(levels_around(), 4200.0, "mixed", 4.0)[0]["direction"] == "short"


def test_a_scenario_without_a_sourced_level_or_target_is_unavailable_never_invented():
    only_one_above = [lv for lv in levels_around() if lv["type"] == "support" or lv["price"] < 4220]
    a, b, c = o.build_scenarios(only_one_above, 4200.0, "bearish", 4.0)
    assert a["available"] and not b["available"] and "No sourced level beyond 4,215.2" in b["reason"] and c["available"]
    assert all(not s["available"] and "15-minute" in s["reason"] for s in o.build_scenarios(levels_around(), 4200.0, "bearish", None))
    assert [s["available"] for s in o.build_scenarios([], 4200.0, None, 4.0)] == [False, False, False]


def published(**changes) -> dict:
    slot = o.current_slot(CFG, MONDAY)
    sources = [{"id": "a", "title": "A", "type": "technical", "url": "https://example.com", "ok": True, "weight": 1.0}, {"id": "b", "title": "B", "type": "technical", "url": None, "ok": True, "weight": 1.0},
               {"id": "cal", "title": "Cal", "type": "calendar", "url": None, "ok": False, "error": "HTTP 503", "weight": 1.0}]
    readings = {"a": Reading(tuple(RawLevel(4200.0 + d, "L", "1D") for d in (-40, -20, 15, 30, 55)), Lean("bearish", "below the pivot")), "b": Reading((RawLevel(4215.5, "M", "4H"),), None)}
    outlook = o.build_outlook(cfg=CFG, slot=slot, outlook_id=slot.id, trigger="scheduled", now=MONDAY, price=4200.0, atr_15m=4.0, sources=sources, readings=readings,
                              events={"cal": [RawEvent("Consumer Price Index", MONDAY + timedelta(hours=2), None, "USD")]}, headlines={})
    outlook.update(changes)
    return outlook


def test_a_complete_brief_passes_every_gate():
    outlook = published()
    assert o.outlook_problems(outlook) == []
    assert (outlook["id"], outlook["session"], outlook["generatedAt"], outlook["validUntil"]) == ("2026-10-12-london", "london", "2026-10-12T07:00:00Z", "2026-10-12T13:15:00Z")
    assert outlook["label"] == "Scenario outlook, not news" and "Not financial advice" in outlook["disclaimer"] and outlook["display"] == "XAU/USD"
    assert (outlook["sourcesAvailable"], outlook["sourcesTotal"], outlook["flags"]) == (2, 3, {"lowConfidence": False, "thinLiquidity": False})
    assert outlook["bias"]["direction"] == "bearish" and outlook["drivers"][0]["label"] == "US data" and outlook["nextHighImpact"]["key"] == "cpi"
    assert [n["kind"] for n in outlook["riskNotes"]] == ["liquidity", "spread", "sizing"] and "US CPI (Mon 12 Oct 16:00 WIB)" in outlook["riskNotes"][1]["text"]
    assert all(lv["sources"] and lv["confluence"] != lv["singleSource"] for lv in outlook["levels"])


def test_a_brief_that_fails_a_gate_is_not_published():
    good = published()
    no_source = {**good, "levels": [{**good["levels"][0], "sources": []}] + good["levels"][1:]}
    assert any("has no source" in p for p in o.outlook_problems(no_source))
    mistagged = {**good, "levels": [{**good["levels"][0], "confluence": True, "singleSource": False}] + good["levels"][1:]}
    assert any("tagged wrongly" in p for p in o.outlook_problems(mistagged))
    wrong_side = {**good, "scenarios": [{**good["scenarios"][0], "stop": 4100.0}] + good["scenarios"][1:]}
    assert any("scenario A stop" in p for p in o.outlook_problems(wrong_side))
    assert any("no source" in p for p in o.outlook_problems({**good, "drivers": [{"label": "Fed", "text": "x", "sources": []}]}))
    assert "validUntil is not later than generatedAt" in o.outlook_problems({**good, "validUntil": good["generatedAt"]})
    assert "the label or the disclaimer is missing" in o.outlook_problems({**good, "label": "Signals"})
    assert "no scenario could be built from the sourced levels" in o.outlook_problems({**good, "scenarios": [{"key": "A", "available": False}]})
    assert "no level could be sourced" in o.outlook_problems({**good, "levels": []})
    low = published()
    assert o.build_outlook.__name__ and low["levelSourcesAvailable"] == 2
    one_source = o.OutlookConfig(enabled=True, min_level_sources=3)
    assert o.build_outlook(cfg=one_source, slot=o.current_slot(CFG, MONDAY), outlook_id="x", trigger="manual", now=MONDAY, price=4200.0, atr_15m=4.0,
                           sources=good["sources"], readings={"a": Reading((RawLevel(4215.0, "L", "1D"),), None)}, events={}, headlines={})["flags"]["lowConfidence"]


# ---------------------------------------------------------------- outcomes (§5.8)
START = 1_760_000_000_000
STEP = 300_000


def series(*bars: tuple[float, float, float]) -> list[Bar]:
    return [bar(START + index * STEP, low, high, close) for index, (low, high, close) in enumerate(bars)]


def test_outcomes_from_the_five_minute_candles():
    a, b, _ = o.build_scenarios(levels_around(), 4200.0, "bearish", 4.0)
    end, late = START + 20 * STEP, START + 21 * STEP
    quiet = series((4198, 4205, 4202), (4199, 4206, 4203))
    assert o.evaluate(a, quiet, START, end, late)["outcome"] == "not_triggered" and o.evaluate(a, quiet, START, end, START + 2 * STEP)["state"] == "watching"
    # A (short from 4211.2-4215.2, stop 4217.2, target 4180): touched, then down to the target
    won = o.evaluate(a, series((4200, 4212, 4208), (4190, 4209, 4192), (4179, 4193, 4181)), START, end, late)
    assert (won["state"], won["outcome"], won["triggeredAt"], won["resolvedAt"]) == ("target", "target", START, START + 3 * STEP - 1)
    # touched and stopped in the same candle; and a later close back above the level
    assert o.evaluate(a, series((4200, 4218, 4210)), START, end, late)["outcome"] == "invalidation"
    assert o.evaluate(a, series((4200, 4213, 4210), (4209, 4216.5, 4216)), START, end, late)["outcome"] == "invalidation"
    # a candle that reaches both the stop and the target counts as invalidated
    assert o.evaluate(a, series((4200, 4213, 4210), (4175, 4218, 4190)), START, end, late)["outcome"] == "invalidation"
    open_trade = o.evaluate(a, series((4200, 4213, 4210), (4205, 4212, 4207)), START, end, late)
    assert (open_trade["state"], open_trade["outcome"]) == ("triggered", "expired_open")
    assert o.evaluate(a, series((4200, 4213, 4210), (4205, 4212, 4207)), START, end, START + 2 * STEP)["outcome"] is None  # still running
    # B (long on a 5m close above 4215.2): a wick is not a close; an unfinished candle cannot close
    wick = series((4205, 4219, 4214), (4213, 4218, 4217), (4216, 4231, 4229))
    assert o.evaluate(b, wick[:1], START, end, late)["state"] == "watching"
    assert o.evaluate(b, wick[:2], START, end, START + 2 * STEP - 5)["state"] == "watching"
    hit = o.evaluate(b, wick, START, end, late)
    assert (hit["outcome"], hit["triggeredAt"]) == ("target", START + 2 * STEP - 1)
    # candles from before the brief, or after it ran out, never count
    assert o.evaluate(a, [bar(START - STEP, 4200, 4218, 4210)] + quiet, START, end, late)["outcome"] == "not_triggered"
    assert o.evaluate(a, quiet + [bar(end, 4200, 4218, 4210)], START, end, late + STEP)["outcome"] == "not_triggered"
    assert o.evaluate({"key": "B", "available": False}, wick, START, end, late)["state"] is None


def test_summary_counts_outcomes_by_session_scenario_and_week():
    rows = [{"session": "london", "key": "A", "outcome": "target", "generatedAt": "2026-10-12T07:00:00Z"}, {"session": "london", "key": "B", "outcome": "not_triggered", "generatedAt": "2026-10-12T07:00:00Z"},
            {"session": "asia", "key": "A", "outcome": "invalidation", "generatedAt": "2026-10-05T00:00:00Z"}, {"session": "asia", "key": "A", "outcome": "target", "generatedAt": "2026-10-18T23:50:00Z"}]
    summary = o.summarise(rows, CFG)
    assert summary["total"] == 4 and [(r["key"], r["total"]) for r in summary["bySession"]] == [("asia", 2), ("london", 2)]
    assert summary["byScenario"][0] == {"key": "A", "not_triggered": 0, "target": 2, "invalidation": 1, "expired_open": 0, "total": 3}
    assert [(r["key"], r["total"]) for r in summary["byWeek"]] == [("2026-10-19", 1), ("2026-10-12", 2), ("2026-10-05", 1)]  # Sunday 23:50 UTC is Monday in WIB
    assert "win" not in str(summary).lower() and o.PAST_CAPTION == "Past outcomes do not predict future results"


# ---------------------------------------------------------------- the user's own version (§5.7)
def test_an_edit_is_checked_like_a_generated_scenario():
    a, b, _ = o.build_scenarios(levels_around(), 4200.0, "bearish", 4.0)
    edited, errors = o.apply_edit(a, {"key": "A", "trigger": " My trigger ", "entryZone": [4210, 4216], "stop": 4219, "targets": [4170, 4185], "invalidation": 4217})
    assert not errors and (edited["trigger"], edited["entryZone"], edited["stop"], edited["targets"], edited["edited"]) == ("My trigger", [4210.0, 4216.0], 4219.0, [4185.0, 4170.0], True)
    assert edited["invalidation"] == "5m close above 4,217" and edited["triggerRule"]["price"] == 4210.0 and edited["direction"] == "short"
    assert edited["riskReward"] == pytest.approx((4213 - 4185) / 6, abs=0.01) and a["stop"] == 4217.2  # the generated scenario is untouched
    moved, _ = o.apply_edit(b, {"entryZone": [4217, 4221]})
    assert moved["triggerRule"] == {"type": "close_above", "price": 4217.0, "timeframe": "5m"}  # the trigger follows the edge of the zone
    for edit, field in (({"stop": 4100}, "stop"), ({"targets": [4300]}, "targets"), ({"entryZone": [4216, 4210]}, "entryZone"), ({"invalidation": 4200}, "invalidation"),
                        ({"invalidation": 4230}, "invalidation"), ({"targets": []}, "targets"), ({"targets": [1, 2, 3, 4]}, "targets"), ({"trigger": " "}, "trigger"),
                        ({"stop": "4219"}, "stop"), ({"stop": True}, "stop"), ({"stop": float("nan")}, "stop"), ({"side": "long"}, "edit")):
        result, errors = o.apply_edit(a, edit)
        assert result is None and field in errors, (edit, errors)
    assert o.apply_edit({"key": "B", "available": False}, {})[1] and o.apply_edit(a, "x")[1]


def test_custom_levels_are_validated():
    clean, errors = o.clean_levels([{"price": 4150, "type": "support", "note": " Yesterday's low "}, {"id": 7, "price": 4290.5, "type": "resistance"}])
    assert not errors and clean == [{"id": None, "price": 4150.0, "type": "support", "note": "Yesterday's low"}, {"id": 7, "price": 4290.5, "type": "resistance", "note": ""}]
    for bad in ([{"price": -1, "type": "support"}], [{"price": 4100, "type": "pivot"}], [{"price": "4100", "type": "support"}], [{"price": 4100, "type": "support", "note": "x" * 201}],
                [{"price": 4100, "type": "support", "id": "7"}], ["4100"], {"price": 4100}, [{"price": 4100, "type": "support"}] * 21):
        assert o.clean_levels(bad)[1], bad


# ---------------------------------------------------------------- technical readings
def test_readings_from_candles():
    day_ms = 86_400_000
    monday = int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000)
    daily = [bar(monday + i * day_ms, 4100 + i, 4150 + i, 4120 + i, minutes=1440) for i in range(8)]  # Mon 5 ... Mon 12
    now_ms = monday + 8 * day_ms + 3_600_000  # Tuesday 13 October, 01:00 UTC
    pivots = o.candle_reading("pivots", {"1d": daily}, 4200.0, now_ms)
    last = daily[-1]
    pivot = (last.high + last.low + last.close) / 3
    assert pivots.levels[0] == RawLevel(pivot, "Daily pivot", "1D") and pivots.levels[1].price == pytest.approx(2 * pivot - last.low) and pivots.lean.direction == "bullish"
    ranges = o.candle_reading("ranges", {"1d": daily}, 4130.0, now_ms)
    assert {lv.role: lv.price for lv in ranges.levels} == {"Previous day high": 4157, "Previous day low": 4107, "Previous day close": 4127, "Previous week high": 4154, "Previous week low": 4100}
    assert ranges.lean.direction == "neutral"  # the weekend candles (Sat 10, Sun 11) are not "the previous day"
    assert o.candle_reading("pivots", {"1d": daily[:5]}, 4200.0, monday + 4 * day_ms + 1).levels[0].price == pytest.approx((4153 + 4103 + 4123) / 3)  # a candle still forming is not used
    hour = 3_600_000
    wave = [4200, 4210, 4225, 4212, 4204, 4215, 4232, 4218, 4209, 4220, 4240, 4226, 4214]
    four_hour = [bar(monday + i * 4 * hour, v - 6, v, v - 3, minutes=240) for i, v in enumerate(wave)]
    swings = o.candle_reading("swings", {"4h": four_hour, "1h": []}, 4220.0, monday + 14 * 4 * hour)
    assert sorted(lv.price for lv in swings.levels if lv.role == "Swing high") == [4225, 4232, 4240] and swings.lean.direction == "bullish"
    closes = [bar(monday + i * hour, 4190, 4200, 4195 + (i % 2), minutes=60) for i in range(60)]
    averages = o.candle_reading("averages", {"1h": closes, "4h": []}, 4210.0, monday + 61 * hour)
    assert [lv.role for lv in averages.levels] == ["50 SMA"] and averages.levels[0].price == pytest.approx(4195.5) and averages.lean == Lean("bullish", "Price is above the 1H 50 SMA (4,195.5).")
    assert o.candle_reading("pivots", {}, 4200.0, now_ms) == Reading() and o.atr(closes[:5]) is None
    assert o.atr([bar(i * STEP, 100, 104, 102) for i in range(20)]) == pytest.approx(4.0)
    with pytest.raises(ValueError):
        o.candle_reading("fib", {}, 1.0, 1)


# ---------------------------------------------------------------- source registry and parsers (§8.1, §8.2)
def test_registry_is_configuration():
    default = {s.id: s for s in o.build_registry()}
    assert [s.id for s in default.values() if s.enabled] == ["hl-pivots", "hl-ranges", "hl-swings", "hl-averages", "bls-calendar", "fed-calendar"]
    assert not default["ff-calendar"].enabled and not default["investing-commodities"].enabled and "terms of use" in default["ff-calendar"].license
    custom = {s.id: s for s in o.build_registry(
        [{"id": "desk-levels", "kind": "levels_json", "url": "https://example.com/levels.json", "weight": 2, "offset": -3.5, "priority": 5}, {"id": "hl-averages", "weight": 0.5}],
        enable=["ff-calendar"], disable=["hl-swings"])}
    assert list(custom)[0] == "desk-levels" and custom["desk-levels"].type == "technical" and custom["desk-levels"].offset == -3.5
    assert custom["hl-averages"].weight == 0.5 and custom["ff-calendar"].enabled and not custom["hl-swings"].enabled
    for bad in ([{"id": "x", "kind": "rss"}], [{"id": "x", "kind": "rss", "url": "http://example.com/feed"}], [{"id": "X y", "kind": "pivots"}], [{"id": "x", "kind": "html"}],
                [{"id": "x", "kind": "pivots", "weight": 0}], [{"id": "x", "kind": "pivots", "colour": "red"}], [{"kind": "pivots"}], [{"id": "x", "kind": "pivots", "type": "rumour"}]):
        with pytest.raises(ValueError):
            o.build_registry(bad)
    with pytest.raises(ValueError, match="unknown source 'nope'"):
        o.build_registry(enable=["nope"])


ICS = b"""BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261014T083000\r\nSUMMARY:Consumer Price\r\n  Index\r\nEND:VEVENT\r\n
BEGIN:VEVENT\r\nDTSTART:20261015T123000Z\r\nSUMMARY:Producer Price Index\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261016\r\nSUMMARY:All day\r\nEND:VEVENT\r\n
BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20270514T083000\r\nSUMMARY:Far away\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
FED = ('﻿{"events": [{"title": "FOMC Meeting", "time": "2:00 p.m.", "month": "2026-10", "days": "27-28", "type": "FOMC"},'
       '{"title": "Speech - Chair Warsh", "time": "11:30 p.m.", "month": "2026-10", "days": "15", "type": "Speeches"},'
       '{"title": "G.17", "time": "9:15 a.m.", "month": "2026-10", "days": "16", "type": "Stat"}, {"title": "No time", "month": "2026-10", "days": "20", "type": "Speeches"}, "junk"]}').encode()
WEEK = b'[{"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"}, {"title": "GDP", "country": "GBP", "date": "2026-10-14T02:00:00-04:00", "impact": "High"}, {"title": "x", "country": "USD", "date": "soon"}]'
RSS = b"""<?xml version="1.0"?><rss><channel><title>Feed</title><item><title> Gold  slips &amp; dollar firms </title><link>https://example.com/gold</link><pubDate>Mon, 12 Oct 2026 06:00:00 GMT</pubDate></item>
<item><title>Old</title><link>https://example.com/old</link><pubDate>2026-10-01 09:00:00</pubDate></item><item><title>No safe link</title><link>javascript:alert(1)</link></item></channel></rss>"""


def test_parsers_read_the_real_formats_and_keep_only_what_they_need():
    assert src.parse_ics(ICS, MONDAY) == [RawEvent("Consumer Price Index", at("2026-10-14T12:30"), None, "USD"), RawEvent("Producer Price Index", at("2026-10-15T12:30"), None, "USD")]
    assert src.parse_fed_json(FED, MONDAY) == [RawEvent("FOMC Meeting", at("2026-10-28T18:00"), None, "USD"), RawEvent("Speech - Chair Warsh", at("2026-10-16T03:30"), None, "USD")]
    assert src.parse_calendar_json(WEEK, MONDAY) == [RawEvent("CPI m/m", at("2026-10-14T12:30"), "high", "USD")]
    assert src.parse_rss(RSS, MONDAY) == [Headline("Gold slips & dollar firms", "https://example.com/gold", at("2026-10-12T06:00"))]
    spec = o.SourceSpec("desk", "Desk", "technical", "levels_json", url="https://example.com/l.json", offset=2.0)
    reading = src.parse_levels_json(b'{"levels": [{"price": 4220, "role": "Fib 23.6%", "timeframe": "4H"}, {"price": "x"}, {"price": -1}], "bias": {"direction": "bearish", "reason": "capped"}}', spec)
    assert reading == Reading((RawLevel(4222.0, "Fib 23.6%", "4H"),), Lean("bearish", "capped"))
    for parser, junk in ((src.parse_fed_json, b"<html>"), (src.parse_calendar_json, b"nope"), (src.parse_rss, b"<rss><channel><item>"), (lambda data, now: src.parse_levels_json(data, spec), b"[]")):
        with pytest.raises(src.SourceError):
            parser(junk, MONDAY)


@pytest.mark.parametrize("hostile", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><rss><channel><item><title>&lol2;</title><link>https://x.example</link></item></channel></rss>',
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title><link>https://x.example</link></item></channel></rss>',
    b'<?xml version="1.0"?><!DOCTYPE rss SYSTEM "https://evil.example/rss.dtd"><rss/>',
])
def test_a_feed_that_declares_a_document_type_or_entities_is_refused(hostile):
    with pytest.raises(src.SourceError, match="document type or entities"):
        src.parse_rss(hostile, MONDAY)


async def test_one_source_failing_never_fails_the_others():
    async def fetch(url: str) -> bytes:
        if "bls" in url:
            return ICS
        if "federalreserve" in url:
            raise TimeoutError
        raise src.SourceError("HTTP 503")

    specs = [s for s in o.build_registry(enable=["ff-calendar"]) if s.enabled]
    results = {r.spec.id: r for r in await src.SourceReader(fetch).read_all(specs, candles={}, candle_error="the exchange did not return candles", price=4200.0, now=MONDAY)}
    assert results["bls-calendar"].ok and len(results["bls-calendar"].events) == 2
    assert (results["fed-calendar"].ok, results["fed-calendar"].error) == (False, "did not answer in time")
    assert results["ff-calendar"].error == "HTTP 503" and results["hl-pivots"].error == "the exchange did not return candles"


# ---------------------------------------------------------------- settings
def load(env):
    return load_settings({"TELEGRAM_ENABLED": "false", **env}, env_file=None)


def test_outlook_settings_defaults_and_overrides(tmp_path):
    cfg = load({}).outlook
    assert cfg.enabled and cfg.coin == "xyz:GOLD" and cfg.instrument == "XAUUSD" and cfg.sessions == o.DEFAULT_SESSIONS and (cfg.tz_name, cfg.tz_offset_minutes) == ("WIB", 420)
    assert (cfg.confluence_tolerance, cfg.carry_distance, cfg.min_level_sources, cfg.history_days) == (5.0, 50.0, 2, 365)
    assert o.OutlookConfig().enabled is False  # a Settings built in code starts nothing that reaches the network
    registry = tmp_path / "sources.json"
    registry.write_text('[{"id": "desk", "kind": "levels_json", "url": "https://example.com/l.json"}]')
    cfg = load({"OUTLOOK_SESSIONS": '{"asia": ["06:45", "07:00"], "london": ["14:45", "15:00"], "newyork": ["21:15", "21:30"]}', "OUTLOOK_CONFLUENCE_TOLERANCE": "3",
                "OUTLOOK_SOURCES_FILE": str(registry), "OUTLOOK_SOURCES_ENABLE": "ff-calendar, investing-commodities", "OUTLOOK_SOURCES_DISABLE": "hl-averages",
                "OUTLOOK_TZ_NAME": "SGT", "OUTLOOK_TZ_OFFSET_MINUTES": "480", "OUTLOOK_HISTORY_DAYS": "0"}).outlook
    enabled = {s.id for s in cfg.sources if s.enabled}
    assert cfg.sessions["london"] == ("14:45", "15:00") and cfg.confluence_tolerance == 3.0 and cfg.tz == timedelta(hours=8) and cfg.history_days == 0
    assert {"desk", "ff-calendar", "investing-commodities"} <= enabled and "hl-averages" not in enabled
    assert load({"OUTLOOK_ENABLED": "false"}).outlook.enabled is False
    silver = load({"HL_SYMBOL_MAP": '{"SILVER": "xyz:SILVER"}'})
    assert silver.outlook.enabled is False and not silver.warnings  # no gold market is managed: off, quietly
    other = load({"OUTLOOK_COIN": "xyz:PLATINUM"})
    assert other.outlook.enabled is False and "OUTLOOK_COIN=xyz:PLATINUM" in other.warnings[0]
    assert "sources" in load({}).public_dict()["outlook"]


@pytest.mark.parametrize("env, named", [
    ({"OUTLOOK_SESSIONS": "{}"}, "OUTLOOK_SESSIONS"), ({"OUTLOOK_SESSIONS": "not json"}, "OUTLOOK_SESSIONS"), ({"OUTLOOK_SOURCES_ENABLE": "nope"}, "unknown source 'nope'"),
    ({"OUTLOOK_SOURCES_FILE": "/nonexistent/sources.json"}, "OUTLOOK_SOURCES_FILE"), ({"OUTLOOK_CONFLUENCE_TOLERANCE": "0"}, "OUTLOOK_CONFLUENCE_TOLERANCE"),
    ({"OUTLOOK_MIN_LEVEL_SOURCES": "none"}, "OUTLOOK_MIN_LEVEL_SOURCES"),
])
def test_a_malformed_outlook_setting_stops_startup_and_names_it(env, named):
    with pytest.raises(ConfigError, match=named):
        load(env)
