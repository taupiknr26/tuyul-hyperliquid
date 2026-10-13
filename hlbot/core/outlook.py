"""Scenario outlook service (docs/outlook.md §9, §10).

Schedules the three briefs of each weekday, reads the sources, builds
and validates the brief, stores it, follows each scenario against the
existing price feed and settles its outcome when the brief runs out.
It also keeps every user's own layer ("My Version").

Separate from trading on purpose: it sends no order, the engine never
reads it, and a failure here is logged and retried, never allowed to
stop the bot. Its loop therefore catches everything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

from hlbot.db.database import dumps, loads
from hlbot.domain.outlook import (
    CANDLE_NEEDS,
    DISCLAIMER,
    LABEL,
    NOTE_MAX,
    OUTCOME_TEXT,
    PAST_CAPTION,
    SESSION_NAMES,
    SESSION_ORDER,
    Slot,
    apply_edit,
    atr,
    build_outlook,
    clean_levels,
    closed_candles,
    current_slot,
    evaluate,
    iso,
    next_slot,
    outlook_problems,
    parse_iso,
    status_of,
    summarise,
    to_ms,
    week_start,
)
from hlbot.exchange.base import ExchangeError
from hlbot.ingest.outlook_sources import SourceReader
from hlbot.timeutil import wait_event

if TYPE_CHECKING:  # pragma: no cover
    from hlbot.core.app import BotApp

logger = logging.getLogger("hlbot.outlook")

TICK_S = 20.0
RETRY_MS = 120_000             # a brief that could not be published is tried again this much later
TRACK_EVERY_MS = 60_000        # scenario states are refreshed from the candles this often
SETTLE_AFTER_MS = 6 * 60_000   # outcomes are settled once the last 5-minute candle of the brief has closed
MAX_TRACK_AGE_MS = 10 * 86_400_000
SCHEDULED_GRACE = timedelta(minutes=5)
QUOTE_FRESH_MS = 60_000
FIVE_MIN_MS = 300_000
HISTORY_LIMIT = 300
FAILURE_KEY = "outlook.failure"


class OutlookError(Exception):
    """A request that cannot be served. ``errors`` names the fields at fault."""

    def __init__(self, message: str, status: int = 400, errors: Optional[dict[str, str]] = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or {}


class OutlookService:
    def __init__(self, bot: BotApp, reader: Optional[SourceReader] = None, tick_s: float = TICK_S):
        self.bot = bot
        self.cfg = bot.s.outlook
        self.db = bot.db
        self.clock = bot.clock
        self.reader = reader or SourceReader()
        self._tick_s = tick_s
        self._lock = asyncio.Lock()
        self._last_attempt_ms = 0
        self._last_manual_ms = 0
        self._tracked_ms = 0
        self._pruned_day = ""

    # ================================================================== loop
    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the outlook must never take the bot down
                logger.exception("outlook pass failed")
            await wait_event(stop_event, self._tick_s)

    async def close(self) -> None:
        await self.reader.close()

    async def tick(self) -> None:
        now = self.clock.now()
        now_ms = to_ms(now)
        self._check_events(now)
        slot = current_slot(self.cfg, now)
        if slot is not None:
            row = self.db.one("SELECT stale_kind FROM outlooks WHERE slot_id = ? ORDER BY generated_ms DESC LIMIT 1", (slot.id,))
            due = row is None or row["stale_kind"] == "event"
            if due and now_ms - self._last_attempt_ms >= RETRY_MS:
                await self.generate("event" if row else "scheduled" if now - slot.start <= SCHEDULED_GRACE else "catchup")
        await self._track(now)
        self._prune(now)

    # ================================================================== generation
    def _price(self, candles: Mapping[str, Sequence[Any]], now_ms: int) -> Optional[float]:
        """The existing live price: the quote the trading side already holds, else the last 15-minute close."""
        quote = self.bot.engine.quotes.get(self.cfg.coin)
        if quote is not None and quote.bid and quote.ask and now_ms - quote.received_ms <= QUOTE_FRESH_MS:
            return (quote.bid + quote.ask) / 2
        recent = sorted(candles.get("15m", ()), key=lambda c: c.open_ms)
        return recent[-1].close if recent else None

    async def _get_candles(self, interval: str, count: int) -> list[Any]:
        try:
            return await self.bot.adapter.get_candles(self.cfg.coin, interval, count)
        except ExchangeError:  # a read can simply be asked again
            return await self.bot.adapter.get_candles(self.cfg.coin, interval, count)

    async def _candles(self) -> tuple[dict[str, list[Any]], Optional[str]]:
        candles: dict[str, list[Any]] = {}
        try:
            for interval, count in CANDLE_NEEDS.items():
                candles[interval] = await self._get_candles(interval, count)
        except ExchangeError as exc:
            return candles, "the exchange did not answer in time" if "timeout" in str(exc).lower() else "the exchange did not return candles"
        return candles, None

    def _failed(self, now: datetime, problems: Sequence[str], sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "at": iso(now), "problems": list(problems)[:8],
            "sourcesAvailable": sum(1 for s in sources if s.get("ok")), "sourcesTotal": len(sources),
        }
        self.db.kv_set(FAILURE_KEY, failure)
        self.db.audit("outlook.failed", failure)
        logger.warning("outlook not published: %s", "; ".join(failure["problems"]))
        return {"ok": False, "error": "the brief could not be published: " + "; ".join(failure["problems"][:3]), **failure}

    async def generate(self, trigger: str, by: Optional[str] = None) -> dict[str, Any]:
        """Read the sources and publish a brief for the session that is
        running. Nothing is published unless it passes every gate."""
        async with self._lock:
            now = self.clock.now()
            now_ms = to_ms(now)
            self._last_attempt_ms = now_ms
            slot = current_slot(self.cfg, now)
            if slot is None:
                return {"ok": False, "error": "no brief is scheduled now: briefs run Monday to Friday"}
            candles, candle_error = await self._candles()
            price = self._price(candles, now_ms)
            if price is None:
                return self._failed(now, ["no price is available from the market feed"], [])
            specs = [spec for spec in self.cfg.sources if spec.enabled]
            results = await self.reader.read_all(specs, candles=candles, candle_error=candle_error, price=price, now=now)
            sources = [
                {
                    "id": r.spec.id, "title": r.spec.title, "type": r.spec.type,
                    "url": r.spec.link or r.spec.url or f"https://app.hyperliquid.xyz/trade/{self.cfg.coin}",
                    "fetchedAt": iso(r.fetched_at), "ok": r.ok, "error": r.error, "weight": r.spec.weight, "priority": r.spec.priority,
                    "license": r.spec.license,
                }
                for r in results
            ]
            if not any(r.ok for r in results):
                return self._failed(now, ["no source answered"], sources)
            taken = int(self.db.scalar("SELECT COUNT(*) FROM outlooks WHERE slot_id = ?", (slot.id,), default=0))
            outlook = build_outlook(
                cfg=self.cfg, slot=slot, outlook_id=slot.id if not taken else f"{slot.id}-{taken + 1}", trigger=trigger, now=now, price=price,
                atr_15m=atr(closed_candles(candles.get("15m", ()), now_ms)), sources=sources,
                readings={r.spec.id: r.reading for r in results if r.ok}, events={r.spec.id: r.events for r in results if r.ok and r.events},
                headlines={r.spec.id: r.headlines for r in results if r.ok and r.headlines},
            )
            problems = outlook_problems(outlook)
            if problems:
                return self._failed(now, problems, sources)
            self._store(outlook, slot, now, price, by)
            return {"ok": True, "id": outlook["id"]}

    def _store(self, outlook: dict[str, Any], slot: Slot, now: datetime, price: float, by: Optional[str]) -> None:
        now_ms = to_ms(now)
        with self.db.tx():
            # a brief published while an earlier one still runs replaces it: the earlier one's outcomes are read up to now
            self.db.execute(
                "UPDATE outlooks SET stale_at = ?, stale_kind = 'replaced', stale_reason = 'Replaced by a newer brief.' "
                "WHERE stale_at IS NULL AND valid_until_ms > ?", (iso(now), now_ms),
            )
            self.db.execute("UPDATE outlooks SET end_ms = MIN(end_ms, ?) WHERE final = 0", (now_ms,))
            self.db.execute(
                "INSERT INTO outlooks (id, slot_id, instrument, session, trigger, generated_at, generated_ms, valid_until, valid_until_ms, end_ms, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (outlook["id"], slot.id, outlook["instrument"], slot.session, outlook["trigger"], outlook["generatedAt"], now_ms,
                 outlook["validUntil"], to_ms(slot.end), to_ms(slot.end), dumps(outlook)),
            )
            # a user's level carries over while it is near price; one left far behind is archived, not deleted (§5.7)
            self.db.execute(
                "UPDATE outlook_user_levels SET archived_at = ?, archived_by = ? WHERE archived_at IS NULL AND ABS(price - ?) > ?",
                (iso(now), outlook["id"], price, self.cfg.carry_distance),
            )
            self.db.kv_set(FAILURE_KEY, None)
            self.db.audit("outlook.published", {
                "id": outlook["id"], "trigger": outlook["trigger"], "by": by, "sources": f"{outlook['sourcesAvailable']}/{outlook['sourcesTotal']}",
                "levels": len(outlook["levels"]), "low_confidence": outlook["flags"]["lowConfidence"],
            })
        logger.info("outlook %s published (%s, %s of %s sources)", outlook["id"], outlook["trigger"], outlook["sourcesAvailable"], outlook["sourcesTotal"])

    async def refresh(self, by: str) -> dict[str, Any]:
        """The "Refresh outlook" button: rate-limited (§10)."""
        now_ms = self.clock.now_ms()
        wait_ms = self._last_manual_ms + self.cfg.manual_refresh_cooldown_s * 1000 - now_ms
        if wait_ms > 0:
            raise OutlookError(f"refreshed a moment ago; try again in {int(wait_ms / 1000) + 1} s", 429)
        self._last_manual_ms = now_ms
        return await self.generate("manual", by)

    # ================================================================== early invalidation and outcomes
    def _mark_stale(self, outlook_id: str, kind: str, reason: str, now: datetime) -> None:
        self.db.execute("UPDATE outlooks SET stale_at = ?, stale_kind = ?, stale_reason = ? WHERE id = ? AND stale_at IS NULL", (iso(now), kind, reason, outlook_id))
        self.db.audit("outlook.stale", {"id": outlook_id, "kind": kind, "reason": reason})
        if kind == "event":
            self._last_attempt_ms = 0  # the refreshed brief follows on the next pass

    def _check_events(self, now: datetime) -> None:
        """A high-impact release makes the running brief stale; the next pass publishes a refreshed one (§10)."""
        now_ms = to_ms(now)
        row = self.db.one("SELECT id, generated_ms, body FROM outlooks WHERE stale_at IS NULL AND valid_until_ms > ? ORDER BY generated_ms DESC LIMIT 1", (now_ms,))
        if row is None:
            return
        body = loads(row["body"], {})
        delay_ms = self.cfg.event_refresh_delay_min * 60_000
        for event in body.get("events", []):
            at_ms = to_ms(parse_iso(event["timeUtc"]))
            if event.get("impact") == "high" and row["generated_ms"] < at_ms and at_ms + delay_ms <= now_ms:
                self._mark_stale(row["id"], "event", f"High-impact release: {event['name']}.", now)
                return

    async def _track(self, now: datetime) -> None:
        now_ms = to_ms(now)
        if now_ms - self._tracked_ms < TRACK_EVERY_MS:
            return
        self.db.execute("UPDATE outlooks SET final = 1 WHERE final = 0 AND end_ms < ?", (now_ms - MAX_TRACK_AGE_MS,))  # too old to read back from candles
        rows = self.db.all("SELECT * FROM outlooks WHERE final = 0 ORDER BY generated_ms")
        if not rows:
            return
        self._tracked_ms = now_ms
        span_ms = min(now_ms, max(row["end_ms"] for row in rows)) - min(row["generated_ms"] for row in rows)
        try:
            candles = await self._get_candles("5m", min(5000, max(3, span_ms // FIVE_MIN_MS + 3)))
        except ExchangeError as exc:
            logger.info("outlook tracking skipped: %s", exc)
            return
        for row in rows:
            body = loads(row["body"], {})
            by_key = {scenario["key"]: scenario for scenario in body.get("scenarios", [])}
            states = {key: evaluate(scenario, candles, row["generated_ms"], row["end_ms"], now_ms) for key, scenario in by_key.items() if scenario.get("available")}
            final = now_ms >= row["end_ms"] + SETTLE_AFTER_MS
            with self.db.tx():
                self.db.execute("UPDATE outlooks SET states = ?, final = ?, tracked_ms = ? WHERE id = ?", (dumps(states), int(final), now_ms, row["id"]))
                for layer in self.db.all("SELECT username, edits FROM outlook_user_layers WHERE outlook_id = ? AND edits != '{}'", (row["id"],)):
                    mine: dict[str, Any] = {}
                    for key, edit in loads(layer["edits"], {}).items():
                        edited, _ = apply_edit(by_key.get(key, {}), {k: v for k, v in edit.items() if k != "editedAt"})
                        if edited:  # an edit counts from the moment it was made, never backwards
                            since = max(row["generated_ms"], to_ms(parse_iso(edit["editedAt"])))
                            mine[key] = evaluate(edited, candles, since, row["end_ms"], now_ms)
                    self.db.execute("UPDATE outlook_user_layers SET states = ? WHERE username = ? AND outlook_id = ?", (dumps(mine), layer["username"], row["id"]))
                broke = [states.get(key, {}).get("triggeredAt") for key in ("B", "C")]
                if row["stale_at"] is None and now_ms < row["valid_until_ms"] and all(broke):
                    self._mark_stale(row["id"], "levels", "Both major levels broke: the brief no longer describes this market.", now)

    def _prune(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self.cfg.history_days <= 0 or day == self._pruned_day:
            return
        self._pruned_day = day
        cutoff = to_ms(now - timedelta(days=self.cfg.history_days))
        with self.db.tx():
            self.db.execute("DELETE FROM outlook_user_layers WHERE outlook_id IN (SELECT id FROM outlooks WHERE generated_ms < ?)", (cutoff,))
            self.db.execute("DELETE FROM outlooks WHERE generated_ms < ?", (cutoff,))

    # ================================================================== read
    @staticmethod
    def _stamp(ms: Any) -> Optional[str]:
        return iso(datetime.fromtimestamp(ms / 1000, UTC)) if ms else None

    def _with_state(self, scenario: dict[str, Any], state: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        if state:
            scenario.update(state=state.get("state"), outcome=state.get("outcome"), triggeredAt=self._stamp(state.get("triggeredAt")),
                            resolvedAt=self._stamp(state.get("resolvedAt")))
        return scenario

    def _status(self, row: Mapping[str, Any], now: datetime) -> str:
        return status_of(parse_iso(row["generated_at"]), parse_iso(row["valid_until"]), parse_iso(row["stale_at"]) if row["stale_at"] else None, now)

    def _view(self, row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        body = loads(row["body"], {})
        states = loads(row["states"], {})
        for scenario in body.get("scenarios", []):
            self._with_state(scenario, states.get(scenario["key"]))
        body["status"] = self._status(row, now)
        if row["stale_at"]:
            body["stale"] = {"at": row["stale_at"], "kind": row["stale_kind"], "reason": row["stale_reason"]}
        elif body["status"] == "stale":
            body["stale"] = {"at": row["valid_until"], "kind": "expired", "reason": "Outlook expired."}
        else:
            body["stale"] = None
        body["final"] = bool(row["final"])
        body["editable"] = body["status"] != "stale"
        return body

    def _slot_view(self, slot: Slot) -> dict[str, Any]:
        return {"id": slot.id, "session": slot.session, "sessionName": SESSION_NAMES[slot.session], "at": iso(slot.start), "opensAt": iso(slot.opens), "until": iso(slot.end)}

    def latest(self) -> dict[str, Any]:
        now = self.clock.now()
        now_ms = to_ms(now)
        row = self.db.one("SELECT * FROM outlooks ORDER BY generated_ms DESC LIMIT 1")
        failure = self.db.kv_get(FAILURE_KEY)
        if failure and row and failure["at"] <= row["generated_at"]:
            failure = None
        slot = current_slot(self.cfg, now)
        cooldown_ms = self._last_manual_ms + self.cfg.manual_refresh_cooldown_s * 1000
        closed = slot is None
        return {
            "enabled": True, "now": iso(now), "instrument": self.cfg.instrument, "display": self.cfg.display, "coin": self.cfg.coin,
            "tz": {"name": self.cfg.tz_name, "offsetMinutes": self.cfg.tz_offset_minutes}, "label": LABEL, "disclaimer": DISCLAIMER,
            "outlook": self._view(row, now) if row else None,
            "marketClosed": closed, "slot": self._slot_view(slot) if slot else None, "nextBrief": self._slot_view(next_slot(self.cfg, now)),
            "failure": failure,
            "refreshAvailableAt": self._stamp(cooldown_ms) if self._last_manual_ms and cooldown_ms > now_ms else None,
            "recap": self._summary(week_start(now, self.cfg), None, None) if closed else None,
            "noTradeDefault": {"before": self.cfg.no_trade_before_min, "after": self.cfg.no_trade_after_min},
            "outcomeText": OUTCOME_TEXT, "pastCaption": PAST_CAPTION,
        }

    def get(self, outlook_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM outlooks WHERE id = ?", (outlook_id,))
        if row is None:
            raise OutlookError("no such brief", 404)
        return self._view(row, self.clock.now())

    def _range_ms(self, start: Optional[str], end: Optional[str]) -> tuple[int, int]:
        """Local calendar dates (in the outlook zone) as a half-open range in milliseconds."""

        def day(text: str, name: str) -> datetime:
            try:
                return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC) - self.cfg.tz
            except ValueError:
                raise OutlookError(f"{name} must be a date, YYYY-MM-DD") from None

        low = to_ms(day(start, "from")) if start else 0
        high = to_ms(day(end, "to") + timedelta(days=1)) if end else 1 << 62
        if low >= high:
            raise OutlookError("the start date is after the end date")
        return low, high

    def _rows(self, start: Optional[str], end: Optional[str], session: Optional[str]) -> list[dict[str, Any]]:
        if session and session not in SESSION_ORDER:
            raise OutlookError(f"session must be one of {', '.join(SESSION_ORDER)}")
        low, high = self._range_ms(start, end)
        sql = "SELECT * FROM outlooks WHERE generated_ms >= ? AND generated_ms < ?" + (" AND session = ?" if session else "")
        return self.db.all(sql + " ORDER BY generated_ms DESC LIMIT ?", (low, high, *([session] if session else []), HISTORY_LIMIT))

    def _layers(self, user: str, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self.db.all(f"SELECT * FROM outlook_user_layers WHERE username = ? AND outlook_id IN ({marks})", (user, *ids))
        return {row["outlook_id"]: row for row in rows}

    def history(self, user: str, start: Optional[str], end: Optional[str], session: Optional[str]) -> dict[str, Any]:
        now = self.clock.now()
        rows = self._rows(start, end, session)
        layers = self._layers(user, [row["id"] for row in rows])
        items = []
        for row in rows:
            body, states = loads(row["body"], {}), loads(row["states"], {})
            layer = layers.get(row["id"])
            mine = loads(layer["states"], {}) if layer else {}
            items.append({
                "id": row["id"], "session": row["session"], "sessionName": SESSION_NAMES[row["session"]], "generatedAt": row["generated_at"],
                "validUntil": row["valid_until"], "trigger": row["trigger"], "status": self._status(row, now), "final": bool(row["final"]),
                "bias": (body.get("bias") or {}).get("direction"), "lowConfidence": bool((body.get("flags") or {}).get("lowConfidence")),
                "scenarios": [
                    {"key": s["key"], "name": s["name"], "available": bool(s.get("available")), "direction": s.get("direction"),
                     "state": (states.get(s["key"]) or {}).get("state"), "outcome": (states.get(s["key"]) or {}).get("outcome")}
                    for s in body.get("scenarios", [])
                ],
                "mine": [{"key": key, "state": state.get("state"), "outcome": state.get("outcome")} for key, state in sorted(mine.items())],
                "journalNote": layer["journal_note"] if layer else "",
            })
        return {"items": items, "limit": HISTORY_LIMIT, "outcomeText": OUTCOME_TEXT, "tz": {"name": self.cfg.tz_name, "offsetMinutes": self.cfg.tz_offset_minutes}}

    def _summary(self, start: Optional[str], end: Optional[str], user: Optional[str]) -> dict[str, Any]:
        rows = self._rows(start, end, None)
        generated = [
            {"session": row["session"], "key": key, "outcome": state["outcome"], "generatedAt": row["generated_at"]}
            for row in rows for key, state in loads(row["states"], {}).items() if state.get("outcome")
        ]
        result = {"from": start, "to": end, "generated": summarise(generated, self.cfg), "caption": PAST_CAPTION, "outcomeText": OUTCOME_TEXT}
        if user is not None:
            by_id = {row["id"]: row for row in rows}
            mine = [
                {"session": by_id[layer["outlook_id"]]["session"], "key": key, "outcome": state["outcome"], "generatedAt": by_id[layer["outlook_id"]]["generated_at"]}
                for layer in self._layers(user, list(by_id)).values() for key, state in loads(layer["states"], {}).items() if state.get("outcome")
            ]
            result["mine"] = summarise(mine, self.cfg)
        return result

    def summary(self, user: str, start: Optional[str], end: Optional[str]) -> dict[str, Any]:
        return self._summary(start, end, user)

    # ================================================================== the user's own version
    def _levels(self, user: str, archived: bool) -> list[dict[str, Any]]:
        rows = self.db.all(
            f"SELECT * FROM outlook_user_levels WHERE username = ? AND archived_at IS {'NOT ' if archived else ''}NULL ORDER BY "
            + ("archived_at DESC LIMIT 50" if archived else "price DESC"), (user,),
        )
        return [
            {"id": row["id"], "price": row["price"], "type": row["type"], "note": row["note"], "createdAt": row["created_at"],
             **({"archivedAt": row["archived_at"], "archivedBy": row["archived_by"]} if archived else {})}
            for row in rows
        ]

    def mine(self, user: str, outlook_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM outlooks WHERE id = ?", (outlook_id,))
        if row is None:
            raise OutlookError("no such brief", 404)
        body = loads(row["body"], {})
        layer = self.db.one("SELECT * FROM outlook_user_layers WHERE username = ? AND outlook_id = ?", (user, outlook_id))
        edits = loads(layer["edits"], {}) if layer else {}
        states = loads(layer["states"], {}) if layer else {}
        by_key = {scenario["key"]: scenario for scenario in body.get("scenarios", [])}
        scenarios: dict[str, Any] = {}
        for key, edit in edits.items():
            edited, _ = apply_edit(by_key.get(key, {}), {k: v for k, v in edit.items() if k != "editedAt"})
            if edited:
                scenarios[key] = self._with_state({**edited, "editedAt": edit.get("editedAt")}, states.get(key))
        return {
            "outlookId": outlook_id, "editable": self._status(row, self.clock.now()) != "stale",
            "customLevels": self._levels(user, False), "archivedLevels": self._levels(user, True),
            "scenarioEdits": [{"key": key, **edit} for key, edit in sorted(edits.items())], "scenarios": scenarios,
            "journalNote": layer["journal_note"] if layer else "", "carryDistance": self.cfg.carry_distance,
        }

    def _layer(self, user: str, outlook_id: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO outlook_user_layers (username, outlook_id, updated_at) VALUES (?, ?, ?)", (user, outlook_id, iso(self.clock.now())),
        )

    def save_mine(self, user: str, outlook_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Save a user's layer. Every part is checked first; if anything
        is wrong, nothing at all is saved and each fault is named."""
        row = self.db.one("SELECT * FROM outlooks WHERE id = ?", (outlook_id,))
        if row is None:
            raise OutlookError("no such brief", 404)
        unknown = set(body) - {"customLevels", "scenarioEdits", "journalNote", "clearArchived"}
        if unknown or not body:
            raise OutlookError("give customLevels, scenarioEdits, journalNote and/or clearArchived")
        now = self.clock.now()
        errors: dict[str, str] = {}
        levels: list[dict[str, Any]] = []
        if "customLevels" in body:
            levels, problems = clean_levels(body["customLevels"])
            errors.update(problems)
        edits: dict[str, dict[str, Any]] = {}
        if "scenarioEdits" in body:
            by_key = {scenario["key"]: scenario for scenario in loads(row["body"], {}).get("scenarios", [])}
            listed = body["scenarioEdits"]
            if not isinstance(listed, list):
                errors["scenarioEdits"] = "must be a list"
            elif listed and self._status(row, now) == "stale":
                errors["scenarioEdits"] = "this brief is no longer current: its scenarios cannot be edited"
            else:
                for edit in listed:
                    key = edit.get("key") if isinstance(edit, dict) else None
                    if not isinstance(key, str) or key not in by_key:
                        errors["scenarioEdits"] = "every edit needs the key of a scenario in this brief"
                        continue
                    edited, problems = apply_edit(by_key[key], edit)
                    errors.update({f"scenarioEdits.{key}.{name}": problem for name, problem in problems.items()})
                    if edited:
                        edits[key] = {"trigger": edited["trigger"], "entryZone": edited["entryZone"], "stop": edited["stop"], "targets": edited["targets"],
                                      "invalidation": edited["invalidationRule"]["price"], "editedAt": iso(now)}
        note = body.get("journalNote")
        if "journalNote" in body and (not isinstance(note, str) or len(note) > NOTE_MAX):
            errors["journalNote"] = f"a note of at most {NOTE_MAX} characters"
        if errors:
            raise OutlookError("not saved: " + "; ".join(f"{name}: {problem}" for name, problem in list(errors.items())[:3]), 400, errors)
        with self.db.tx():
            if "customLevels" in body:
                current = {r["id"] for r in self.db.all("SELECT id FROM outlook_user_levels WHERE username = ? AND archived_at IS NULL", (user,))}
                kept = {level["id"] for level in levels if level["id"] in current}
                for level_id in current - kept:
                    self.db.execute("DELETE FROM outlook_user_levels WHERE id = ? AND username = ?", (level_id, user))
                for level in levels:
                    if level["id"] in kept:
                        self.db.execute("UPDATE outlook_user_levels SET price = ?, type = ?, note = ? WHERE id = ? AND username = ?",
                                        (level["price"], level["type"], level["note"], level["id"], user))
                    else:
                        self.db.execute("INSERT INTO outlook_user_levels (username, price, type, note, created_at) VALUES (?, ?, ?, ?, ?)",
                                        (user, level["price"], level["type"], level["note"], iso(now)))
            if body.get("clearArchived") is True:
                self.db.execute("DELETE FROM outlook_user_levels WHERE username = ? AND archived_at IS NOT NULL", (user,))
            if edits or "journalNote" in body:
                self._layer(user, outlook_id)
                layer = self.db.one("SELECT edits, states FROM outlook_user_layers WHERE username = ? AND outlook_id = ?", (user, outlook_id))
                assert layer is not None
                merged = {**loads(layer["edits"], {}), **edits}
                states = {key: state for key, state in loads(layer["states"], {}).items() if key not in edits}  # an edited scenario starts over
                self.db.execute(
                    "UPDATE outlook_user_layers SET edits = ?, states = ?, journal_note = COALESCE(?, journal_note), updated_at = ? WHERE username = ? AND outlook_id = ?",
                    (dumps(merged), dumps(states), note if "journalNote" in body else None, iso(now), user, outlook_id),
                )
        return self.mine(user, outlook_id)

    def reset_scenario(self, user: str, outlook_id: str, key: str) -> dict[str, Any]:
        """Back to the generated values for one scenario."""
        layer = self.db.one("SELECT edits, states FROM outlook_user_layers WHERE username = ? AND outlook_id = ?", (user, outlook_id))
        if layer is not None:
            edits, states = loads(layer["edits"], {}), loads(layer["states"], {})
            edits.pop(key, None)
            states.pop(key, None)
            self.db.execute("UPDATE outlook_user_layers SET edits = ?, states = ?, updated_at = ? WHERE username = ? AND outlook_id = ?",
                            (dumps(edits), dumps(states), iso(self.clock.now()), user, outlook_id))
        return self.mine(user, outlook_id)

    def forget_user(self, user: str) -> None:
        """A removed account leaves no layer behind for a later account of the same name."""
        with self.db.tx():
            self.db.execute("DELETE FROM outlook_user_layers WHERE username = ?", (user,))
            self.db.execute("DELETE FROM outlook_user_levels WHERE username = ?", (user,))
