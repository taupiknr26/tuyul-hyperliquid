"""Signal manager: store, parse, validate, plan, hold waiting signals,
evaluate them on every quote, expire and de-duplicate (PRD §10.1 stages
1-3; FR-002 .. FR-011, FR-020 .. FR-026).

Every record is keyed by ``(channel_id, message_id)``, so equal message
ids from different channels never collide (fixes LIM-01).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from hlbot.config import Settings
from hlbot.core.engine import Engine
from hlbot.core.metrics import Metrics
from hlbot.core.notifier import Notifier
from hlbot.db.database import Database, Row, dumps
from hlbot.domain.parser import PARSER_VERSION, parse_message
from hlbot.domain.planning import (
    allocate_tp_levels,
    apply_price_offset,
    build_content_id,
    compute_effective_range,
    compute_expires_at,
    is_expired,
)
from hlbot.domain.validator import validate_signal
from hlbot.exchange.base import ExchangeAdapter, ExchangeError
from hlbot.logging_setup import new_correlation_id
from hlbot.timeutil import Clock

logger = logging.getLogger("hlbot.signals")


class SignalManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        engine: Engine,
        notifier: Notifier,
        adapter: ExchangeAdapter,
        clock: Optional[Clock] = None,
        metrics: Optional[Metrics] = None,
        config_version: Optional[int] = None,
    ):
        self.s = settings
        self.db = db
        self.engine = engine
        self.notifier = notifier
        self.adapter = adapter
        self.clock = clock or Clock()
        self.metrics = metrics or Metrics()
        self.config_version = config_version
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_refresh: dict[str, float] = {}

    # ------------------------------------------------------------ ingest
    def ingest(self, message: dict[str, Any], source: str = "live") -> Optional[int]:
        """Store one Telegram message and everything derived from it.

        ``message``: ``channel_id``, ``message_id``, ``channel_ref``,
        ``date`` (ISO, Telegram's timestamp), ``text``, ``has_media``,
        ``reply_to``. Returns the signal row id, or None when the
        message was already stored (duplicate delivery or backfill
        overlap)."""
        channel_id = int(message["channel_id"])
        message_id = int(message["message_id"])
        if self.db.one("SELECT 1 FROM messages WHERE channel_id = ? AND message_id = ?", (channel_id, message_id)):
            return None
        new_correlation_id("msg-")
        text = message.get("text") or ""
        now = self.clock.iso()
        record = parse_message(
            {
                "message_id": message_id,
                "channel_id": channel_id,
                "channel_username": message.get("channel_ref"),
                "date": message.get("date"),
                "reply_to_message_id": message.get("reply_to"),
                "text": text,
                "has_media": bool(message.get("has_media")),
            },
            parsed_at=now,
        )
        kind = record["type"]
        parsed = record["parsed"] or {}
        with self.db.tx():
            self.db.insert(
                "messages",
                channel_id=channel_id,
                message_id=message_id,
                channel_ref=None if message.get("channel_ref") is None else str(message["channel_ref"]),
                sent_at=message.get("date"),
                received_at=now,
                text=text,  # stored byte-identical to Telegram's (FR-003)
                has_media=1 if message.get("has_media") else 0,
                reply_to=message.get("reply_to"),
            )
            is_signal = kind == "signal"
            signal_id = self.db.insert(
                "signals",
                channel_id=channel_id,
                message_id=message_id,
                channel_ref=None if message.get("channel_ref") is None else str(message["channel_ref"]),
                type=kind,
                symbol=parsed.get("symbol") if is_signal else None,
                side=parsed.get("action") if is_signal else None,
                parse_errors=dumps(record["parse_errors"]),
                parser_version=PARSER_VERSION,
                state="RECEIVED",
                signal_time=message.get("date") or now,
                created_at=now,
                updated_at=now,
            )
            self.db.audit("message.received", {"type": kind, "source": source, "has_media": bool(message.get("has_media"))}, signal_id=signal_id)
            self.db.set_signal_state(signal_id, "PARSED")
            if not is_signal:
                self.db.set_signal_state(signal_id, "NOT_A_SIGNAL")
                self.metrics.inc("messages_stored")
                return signal_id
            self._plan(signal_id, record, message)
        self.metrics.inc("messages_stored")
        self.metrics.inc("signals_parsed")
        return signal_id

    def _plan(self, signal_id: int, record: dict[str, Any], message: dict[str, Any]) -> None:
        """Validate, map the symbol and build the plan (stage 2). Invalid
        signals are stored with reasons and never traded."""
        parsed = record["parsed"] or {}
        valid, errors = validate_signal(parsed, record["parse_errors"])
        coin = self.s.map_symbol(parsed.get("symbol")) if valid else None
        if valid and coin is None:
            valid, errors = False, [f"unmapped symbol {parsed.get('symbol')!r}: add it to HL_SYMBOL_MAP"]
        instrument = self.engine.instruments.get(coin) if coin else None
        if valid and (instrument is None or instrument.is_delisted):
            valid, errors = False, [f"coin {coin!r} is unknown or delisted on the exchange"]

        zone = parsed.get("entry_zone") or {}
        take_profits = parsed.get("take_profits")
        fields: dict[str, Any] = {
            "zone_low": zone.get("low"),
            "zone_high": zone.get("high"),
            "sl": parsed.get("stop_loss"),
            "tps": dumps(take_profits) if take_profits else None,
            "valid": 1 if valid else 0,
            "validation_errors": dumps(errors),
        }
        signal_time = message.get("date") or self.clock.iso()
        notice = dict(
            message_id=message["message_id"], channel=message.get("channel_ref"), symbol=parsed.get("symbol"),
            action=parsed.get("action"), entry_zone=zone, stop_loss=parsed.get("stop_loss"),
            take_profits=take_profits, valid=valid, errors=errors, coin=coin,
        )
        if not valid:
            self.db.update("signals", signal_id, **fields)
            self.db.set_signal_state(signal_id, "INVALID", "; ".join(errors))
            self.notifier.notify("info_signal", **notice)
            return

        shifted = apply_price_offset(parsed, self.s.price_offset_points)
        buffer = self.s.buffer_value if self.s.buffer_enabled else 0.0
        effective = compute_effective_range(shifted["entry_zone"], buffer)
        tp_levels = allocate_tp_levels(
            shifted["take_profits"], list(self.s.tp_close_percents), shifted["action"], self.s.tp_buffer_value
        )
        expires_at = compute_expires_at(signal_time, self.s.signal_expiry_minutes)
        content_id = build_content_id(parsed["symbol"], parsed["action"], parsed["entry_zone"], signal_time)
        self.db.update("signals", signal_id, content_id=content_id, expires_at=expires_at, **fields)
        assert coin is not None and instrument is not None
        self.db.insert(
            "plans",
            signal_id=signal_id,
            coin=coin,
            asset_id=instrument.asset_id,
            effective_low=effective["lower"],
            effective_high=effective["upper"],
            buffer=buffer,
            sl=shifted["stop_loss"],
            tp_levels=dumps(tp_levels),
            config_version=self.config_version,
        )
        self.db.set_signal_state(signal_id, "PLANNED")
        self.db.set_signal_state(signal_id, "WAITING_PRICE")
        self.notifier.notify("info_signal", expires_at=expires_at, **notice)
        if is_expired(expires_at, self.clock.now()):
            # Backfilled late: stored and reported, never traded (FR-004).
            signal = self.db.get("signals", signal_id)
            assert signal is not None
            self._expire(signal)

    def _expire(self, signal: Row) -> None:
        if self.db.set_signal_state(signal["id"], "EXPIRED", f"not entered before {signal['expires_at']}"):
            self.notifier.notify(
                "signal_skipped", state="EXPIRED", reason=f"not entered before {signal['expires_at']}",
                message_id=signal["message_id"], content_id=signal["content_id"],
            )

    # ------------------------------------------------------------ edits and deletes (FR-005)
    def on_edit(self, channel_id: int, message_id: int, new_text: str, edited_at: Optional[str] = None) -> bool:
        row = self.db.one("SELECT text FROM messages WHERE channel_id = ? AND message_id = ?", (channel_id, message_id))
        if row is None or row["text"] == (new_text or ""):
            return False
        last = self.db.one(
            "SELECT payload FROM audit_events WHERE kind = 'message.edited' AND signal_id = "
            "(SELECT id FROM signals WHERE channel_id = ? AND message_id = ?) ORDER BY seq DESC LIMIT 1",
            (channel_id, message_id),
        )
        if last and f'"new_text": {dumps(new_text)}' in last["payload"]:
            return False  # the same edit delivered twice
        signal = self.db.one("SELECT * FROM signals WHERE channel_id = ? AND message_id = ?", (channel_id, message_id))
        with self.db.tx():
            self.db.execute(
                "UPDATE messages SET edited_at = ? WHERE channel_id = ? AND message_id = ?",
                (edited_at or self.clock.iso(), channel_id, message_id),
            )
            self.db.audit(
                "message.edited", {"old_text": row["text"], "new_text": new_text}, signal_id=signal["id"] if signal else None
            )
        if signal and signal["type"] == "signal":
            self.notifier.alert(
                f"Signal message #{message_id} was edited",
                f"State: {signal['state']}. No automatic change is made to the trade.\n\nOLD:\n{row['text']}\n\nNEW:\n{new_text}",
                critical=False,
            )
        return True

    def on_delete(self, channel_id: Optional[int], message_ids: list[int]) -> int:
        count = 0
        for message_id in message_ids:
            if channel_id is None:
                rows = self.db.all("SELECT channel_id FROM messages WHERE message_id = ? AND deleted_at IS NULL", (message_id,))
                if len(rows) != 1:
                    continue  # ambiguous without the channel: never guess
                channel = rows[0]["channel_id"]
            else:
                channel = channel_id
            row = self.db.one(
                "SELECT text, deleted_at FROM messages WHERE channel_id = ? AND message_id = ?", (channel, message_id)
            )
            if row is None or row["deleted_at"]:
                continue
            signal = self.db.one("SELECT * FROM signals WHERE channel_id = ? AND message_id = ?", (channel, message_id))
            with self.db.tx():
                self.db.execute(
                    "UPDATE messages SET deleted_at = ? WHERE channel_id = ? AND message_id = ?", (self.clock.iso(), channel, message_id)
                )
                self.db.audit("message.deleted", {"text": row["text"]}, signal_id=signal["id"] if signal else None)
            if signal and signal["type"] == "signal":
                self.notifier.alert(
                    f"Signal message #{message_id} was deleted",
                    f"State: {signal['state']}. No automatic change is made to the trade.\n\n{row['text']}", critical=False,
                )
            count += 1
        return count

    # ------------------------------------------------------------ waiting signals
    def waiting(self, coin: Optional[str] = None) -> list[Row]:
        rows = self.db.all(
            "SELECT s.*, p.coin, p.effective_low, p.effective_high FROM signals s JOIN plans p ON p.signal_id = s.id "
            "WHERE s.state = 'WAITING_PRICE' ORDER BY s.id"
        )
        return [r for r in rows if coin is None or r["coin"] == coin]

    def _evaluate(self, signal_id: int) -> None:
        """Start an entry evaluation unless one is already running for
        this signal. Runs as its own task so a quote handler never
        blocks the stream reader."""
        task = self._tasks.get(signal_id)
        if task is not None and not task.done():
            return
        self._tasks[signal_id] = asyncio.get_running_loop().create_task(self._run(signal_id), name=f"entry-{signal_id}")

    async def _fresh_quote(self, coin: str) -> None:
        """Re-confirm the top of book over REST when the last quote is
        ageing. ``bbo`` only pushes on change (observed: nothing for many
        seconds on a quiet market), so an old quote may still be the
        current one; a book read proves it instead of deciding on it
        blind (FR-024). At most one read per coin per window."""
        refresh_after = self.s.risk.max_quote_age_ms * 0.75
        age = self.engine.quote_age_ms(coin)
        if age is not None and age <= refresh_after:
            return
        now = self.clock.monotonic()
        if (now - self._last_refresh.get(coin, -1e12)) * 1000.0 < refresh_after:
            return
        self._last_refresh[coin] = now
        try:
            quote = await self.adapter.get_book(coin)
        except ExchangeError as exc:
            logger.warning("book refresh failed for %s: %s", coin, exc)
            return
        self.metrics.inc("book_refreshes")
        await self.engine.on_quote(quote)

    async def _run(self, signal_id: int) -> None:
        try:
            coin = self.db.scalar("SELECT coin FROM plans WHERE signal_id = ?", (signal_id,))
            if coin:
                await self._fresh_quote(coin)  # a new signal on a quiet market does not wait for the next tick
            await self.engine.try_enter(signal_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("entry evaluation failed for signal %s", signal_id)
            self.metrics.inc("entry_errors")
        finally:
            self._tasks.pop(signal_id, None)

    async def on_quote(self, quote: Any) -> None:
        """FR-023: every best-bid/offer update re-evaluates the waiting
        signals on that coin."""
        for signal in self.waiting(quote.coin):
            self._evaluate(signal["id"])

    async def tick(self) -> None:
        """Once a second: expire, keep the quote fresh while signals
        wait, and re-evaluate."""
        now = self.clock.now()
        waiting = self.waiting()
        for signal in waiting:
            if is_expired(signal["expires_at"], now):
                self._expire(signal)
        for coin in {s["coin"] for s in waiting if not is_expired(s["expires_at"], now)}:
            await self._fresh_quote(coin)
            for signal in self.waiting(coin):
                self._evaluate(signal["id"])

    async def idle(self) -> None:
        """Wait for running evaluations (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        self._tasks.clear()
