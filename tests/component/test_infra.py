"""Notifications outbox, Telegram listener, database guarantees, local
API and CLI (FR-001, FR-004..FR-006, FR-090..FR-094; TC-110..TC-112,
TC-120)."""

import asyncio
import json
import logging
import sqlite3
from datetime import timedelta

import aiohttp
import pytest

from hlbot.config import Settings
from hlbot.core.notifier import Notifier, PermanentSendError, format_notification
from hlbot.db.database import Database, DatabaseCorrupt
from hlbot.domain.states import IllegalTransition
from hlbot.ingest.telegram import ChannelResolutionError, TelegramIngest
from hlbot.timeutil import ManualClock
from tests.component.conftest import CHANNEL, COIN, SELL_TEXT, START, Harness


# ================================================================== database
def fresh_db(tmp_path, clock=None):
    db = Database(tmp_path / "t.sqlite3", clock or ManualClock(START))
    db.migrate()
    return db


def test_migrations_are_forward_only_and_idempotent(tmp_path):
    db = fresh_db(tmp_path)
    assert db.schema_version() == 4
    assert db.migrate() == 4  # nothing to do the second time
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.scalar("PRAGMA synchronous") == 2  # FULL: intents are durable before the request leaves (NFR-008)
    assert db.one("SELECT mode FROM control_state")["mode"] == "RUNNING"
    assert (tmp_path / "t.sqlite3").stat().st_mode & 0o077 == 0
    db.close()


def test_nfr009_audit_rows_can_never_be_updated_or_deleted(tmp_path):
    db = fresh_db(tmp_path)
    seq = db.audit("test.event", {"a": 1})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_events SET kind = 'x' WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_events")
    assert db.one("SELECT kind, payload FROM audit_events WHERE seq = ?", (seq,)) == {"kind": "test.event", "payload": '{"a": 1}'}
    db.close()


def test_audit_payloads_are_redacted(tmp_path):
    db = fresh_db(tmp_path)
    db.audit("x", {"signature": {"r": "0x1"}, "note": "key 0x" + "ab" * 32, "address": "0x" + "12" * 20})
    payload = db.one("SELECT payload FROM audit_events")["payload"]
    assert "ab" * 32 not in payload and "0x" + "12" * 20 not in payload and '"signature": "***"' in payload
    db.close()


def test_a_corrupt_database_is_never_treated_as_empty(tmp_path):
    """Fixes LIM-14: no "starting fresh"."""
    path = tmp_path / "t.sqlite3"
    path.write_bytes(b"this is not a sqlite database, it only looks like a file" * 40)
    with pytest.raises(DatabaseCorrupt):
        Database(path)
    assert path.read_bytes().startswith(b"this is not")  # left untouched for the operator


def test_terminal_order_states_are_immutable_and_contradictions_are_audited(tmp_path):
    db = fresh_db(tmp_path)
    now = db.clock.iso()
    db.insert("messages", channel_id=1, message_id=1, received_at=now, text="x")
    signal = db.insert("signals", channel_id=1, message_id=1, type="signal", state="WAITING_PRICE", created_at=now, updated_at=now)
    trade = db.insert("trades", signal_id=signal, attempt=1, env="paper", coin=COIN, side="SELL", state="ENTERING", planned_size=1, sl_price=1, tp_plan="[]", created_at=now, updated_at=now)
    order = db.insert("orders", trade_id=trade, role="ENTRY", cloid="0x" + "1" * 32, coin=COIN, side="SELL", type="limit", reduce_only=0, price=1, size=1, state="INTENT", created_at=now, updated_at=now)
    with pytest.raises(IllegalTransition):
        db.set_order_state(order, "FILLED")  # nothing fills before it was sent
    assert db.set_order_state(order, "SUBMITTING") and db.set_order_state(order, "FILLED", filled_size=1)
    assert not db.set_order_state(order, "CANCELED")  # a later contradictory report does not rewrite history
    assert db.get("orders", order)["state"] == "FILLED"
    assert db.one("SELECT payload FROM audit_events WHERE kind = 'order.contradiction'")["payload"].count("CANCELED") == 1
    with pytest.raises(IllegalTransition):
        db.set_trade_state(trade, "CLOSED")
    db.set_trade_state(trade, "ABORTED")
    with pytest.raises(IllegalTransition):
        db.set_trade_state(trade, "OPEN_UNPROTECTED")
    with pytest.raises(sqlite3.IntegrityError):  # cloid is unique: the same intent cannot be stored twice
        db.insert("orders", trade_id=trade, role="ENTRY", cloid="0x" + "1" * 32, coin=COIN, side="SELL", type="limit", reduce_only=0, price=1, size=1, state="INTENT", created_at=now, updated_at=now)
    db.close()


def test_transactions_roll_back_as_a_unit_and_backup_is_consistent(tmp_path):
    db = fresh_db(tmp_path)
    with pytest.raises(RuntimeError):
        with db.tx():
            db.kv_set("a", 1)
            db.audit("inside", {})
            raise RuntimeError("boom")
    assert db.kv_get("a") is None and db.scalar("SELECT COUNT(*) FROM audit_events") == 0
    db.kv_set("a", {"x": 1})
    copy = db.backup(tmp_path / "backups" / "copy.sqlite3")
    assert sorted(p.name for p in copy.parent.iterdir()) == ["copy.sqlite3"]  # self-contained: no -wal / -shm
    restored = Database(copy, readonly=True)
    assert restored.kv_get("a") == {"x": 1} and copy.stat().st_mode & 0o077 == 0
    with pytest.raises(sqlite3.OperationalError):
        restored.execute("INSERT INTO kv (key, value, updated_at) VALUES ('b', '1', 'now')")
    restored.close()
    db.close()


# ================================================================== notifier outbox (FR-092)
class Channel:
    def __init__(self):
        self.up = True
        self.sent = []
        self.permanent = None

    async def send(self, text):
        if self.permanent and self.permanent in text:
            raise PermanentSendError("Bot API HTTP 400")
        if not self.up:
            raise RuntimeError("Bot API unreachable: timeout")
        self.sent.append(text)


def make_notifier(tmp_path, clock=None):
    clock = clock or ManualClock(START)
    db = fresh_db(tmp_path, clock)
    channel = Channel()
    return Notifier(db, Settings(env="testnet"), clock, channel.send), channel, db, clock


async def test_tc112_bot_api_down_for_ten_minutes_everything_is_delivered_afterwards_in_order(tmp_path):
    notifier, channel, db, clock = make_notifier(tmp_path)
    channel.up = False
    for index in range(5):
        notifier.alert(f"alert {index}", critical=index == 2)
        await notifier.deliver_due()
        clock.advance(120, monotonic=False)
    assert channel.sent == [] and notifier.pending() == 5
    row = db.one("SELECT attempts, last_error FROM outbox ORDER BY id LIMIT 1")
    assert row["attempts"] >= 1 and "unreachable" in row["last_error"]

    channel.up = True
    clock.advance(600, monotonic=False)
    assert await notifier.deliver_due() == 5
    assert [text.split("alert ")[1][0] for text in channel.sent] == ["0", "1", "2", "3", "4"]
    assert notifier.pending() == 0 and all("[TESTNET]" in text for text in channel.sent)
    db.close()


async def test_undelivered_notifications_survive_a_restart(tmp_path):
    notifier, channel, db, clock = make_notifier(tmp_path)
    channel.up = False
    notifier.notify("service_status", state="started", mode="TESTNET", channel="@c")
    notifier.alert("unprotected position", critical=True)
    await notifier.deliver_due()
    db.close()

    db2 = Database(tmp_path / "t.sqlite3", clock)
    channel2 = Channel()
    clock.advance(400, monotonic=False)
    restarted = Notifier(db2, Settings(env="testnet"), clock, channel2.send)
    assert restarted.pending() == 2 and await restarted.deliver_due() == 2
    assert "Bot Started" in channel2.sent[0] and "unprotected position" in channel2.sent[1]
    db2.close()


async def test_retry_uses_backoff_and_gives_up_after_24_hours(tmp_path):
    notifier, channel, db, clock = make_notifier(tmp_path)
    channel.up = False
    notifier.alert("x")
    await notifier.deliver_due()
    first = db.one("SELECT next_attempt_at FROM outbox")["next_attempt_at"]
    await notifier.deliver_due()  # not due yet: no second attempt
    assert db.one("SELECT attempts FROM outbox")["attempts"] == 1 and first > clock.iso()
    clock.advance(24 * 3600 + 1, monotonic=False)
    await notifier.deliver_due()
    row = db.one("SELECT dropped_at, last_error FROM outbox")
    assert row["dropped_at"] and "24 h" in row["last_error"] and notifier.pending() == 0
    db.close()


async def test_a_message_the_bot_api_rejects_outright_does_not_block_the_queue(tmp_path):
    notifier, channel, db, clock = make_notifier(tmp_path)
    channel.permanent = "poison"
    notifier.alert("poison pill")
    notifier.alert("critical: flatten failed", critical=True)
    assert await notifier.deliver_due() == 1
    assert len(channel.sent) == 1 and "flatten failed" in channel.sent[0]
    assert db.one("SELECT dropped_at FROM outbox ORDER BY id LIMIT 1")["dropped_at"]
    db.close()


async def test_disabled_notifications_are_still_recorded_as_alert_history(tmp_path):
    clock = ManualClock(START)
    db = fresh_db(tmp_path, clock)
    notifier = Notifier(db, Settings(), clock)
    notifier.alert("something happened")
    assert not notifier.enabled and notifier.pending() == 0 and await notifier.deliver_due() == 0
    row = db.one("SELECT text, last_error, dropped_at FROM outbox")
    assert "something happened" in row["text"] and row["last_error"] == "notifications disabled" and "[PAPER]" in row["text"]
    db.close()


def test_notification_text_never_contains_a_secret(tmp_path):
    from hlbot.redact import clear_secrets, register_secrets

    clock = ManualClock(START)
    db = fresh_db(tmp_path, clock)
    token = "987654321:" + "Z" * 35
    register_secrets([token])
    notifier = Notifier(db, Settings(), clock)
    notifier.alert("send failed", f"url https://api.telegram.org/bot{token}/sendMessage key 0x{'ab' * 32}")
    text = db.one("SELECT text FROM outbox")["text"]
    assert token not in text and "ab" * 32 not in text
    clear_secrets()
    db.close()


# ---- formatters: successors of the baseline's formatter tests
def test_formatters_escape_html_and_label_the_environment():
    text = format_notification({"kind": "info_signal", "label": "PAPER", "message_id": 7, "channel": "<b>@chan</b>", "symbol": "GOLD", "action": "SELL",
                                "entry_zone": {"low": 4286.0, "high": 4289.0}, "stop_loss": 4296.0, "take_profits": {"tp2": 4280.0, "tp1": 4283.0},
                                "valid": False, "errors": ["stop_loss must be above entry_zone for a SELL <&>"]})
    assert "&lt;b&gt;@chan&lt;/b&gt;" in text and "&lt;&amp;&gt;" in text and "<b>[PAPER]</b>" in text
    assert text.index("TP1") < text.index("TP2") and "Invalid" in text and "4286 – 4289" in text


def test_every_notification_kind_formats_and_unknown_kinds_raise():
    events = [
        {"kind": "info_signal", "message_id": 1, "valid": True, "symbol": "GOLD", "action": "BUY", "coin": "xyz:GOLD", "expires_at": "t"},
        {"kind": "order_placed", "message_id": 1, "coin": "xyz:GOLD", "direction": "SELL", "size": 0.2412, "avg_entry": 4287.0, "sl": 4296.0,
         "legs": [{"level": 1, "size": 0.0484, "price": 4283.0}], "content_id": "GOLD-SELL-4286-4289-20260914"},
        {"kind": "signal_closed", "outcome": "WIN_PARTIAL", "coin": "xyz:GOLD", "direction": "SELL", "net_pnl": 1.23, "realized_pnl": 2.0, "fees": 0.7,
         "funding": -0.07, "legs": [{"role": "TP", "level": 1, "size": 0.05, "price": 4283.0, "pnl": 0.2}, {"role": "SL", "size": 0.19, "price": 4296.1, "pnl": -1.7}]},
        {"kind": "service_status", "state": "stopped", "mode": "PAPER", "channel": "@c", "reason": "stop flag"},
        {"kind": "daily_target_reached", "today_pnl": 21.5, "daily_target_percent": 2.0, "symbol": "GOLD", "direction": "SELL", "content_id": "x"},
        {"kind": "signal_skipped", "state": "REJECTED_RISK", "reason": "MIN_RR: 0.4 < 0.5", "message_id": 3, "content_id": "x"},
        {"kind": "alert", "title": "HALTED", "detail": "flatten not verified", "critical": True},
    ]
    texts = [format_notification(event) for event in events]
    assert "expires t" in texts[0] and "verified on exchange" in texts[1] and "TP1" in texts[1]
    assert "WIN — PARTIAL" in texts[2] and "+$1.23" in texts[2] and "-$1.70" in texts[2]
    assert "Bot Stopped" in texts[3] and "stop flag" in texts[3] and "target 2%" in texts[4]
    assert "Rejected" in texts[5] and "MIN_RR" in texts[5] and "\U0001f6a8" in texts[6]
    with pytest.raises(ValueError):
        format_notification({"kind": "nope"})


# ================================================================== telegram listener
class FakeMessage:
    def __init__(self, message_id, text, chat_id, date=None, edit_date=None):
        self.id = message_id
        self.message = text
        self.chat_id = chat_id
        self.date = date or START
        self.media = None
        self.reply_to = None
        self.edit_date = edit_date


class FakeEntity:
    def __init__(self, entity_id):
        self.id = entity_id


class FakeTelegram:
    """Stands in for a Telethon client."""

    def __init__(self, channels, history=None):
        self.channels = channels  # ref -> chat id (or an Exception to raise)
        self.history = history or {}
        self.handlers = {}
        self.fail_connects = 0
        self.connect_calls = 0
        self.authorized = True
        self.drop = asyncio.Event()
        self.backfills = 0

    async def connect(self):
        self.connect_calls += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionError("network unreachable")

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, channel):
        value = self.channels[channel]
        if isinstance(value, Exception):
            raise value
        return FakeEntity(value)

    def on(self, builder):
        def register(fn):
            self.handlers[type(builder).__name__] = fn
            return fn

        return register

    def iter_messages(self, entity, limit):
        self.backfills += 1
        messages = list(reversed(self.history.get(entity.id, [])))[:limit]

        async def generate():
            for message in messages:  # newest first, like Telethon
                yield message

        return generate()

    async def run_until_disconnected(self):
        await self.drop.wait()
        self.drop = asyncio.Event()
        raise ConnectionError("connection lost")

    async def disconnect(self):
        pass


def ingest_with(fake, settings, **callbacks):
    received, alerts = [], []
    ingest = TelegramIngest(
        settings,
        on_message=callbacks.get("on_message", lambda message, source: received.append((message["channel_id"], message["message_id"], source))),
        on_edit=callbacks.get("on_edit", lambda *a: None),
        on_delete=callbacks.get("on_delete", lambda *a: None),
        alert=lambda title, detail="", critical=False: alerts.append(title),
        client_factory=lambda s: fake,
        backoff_scale=0.001,
    )
    return ingest, received, alerts


async def test_fr001_one_unresolvable_channel_is_skipped_the_others_are_listened_to():
    fake = FakeTelegram(
        {"@one": 111, "@gone": ValueError("no such channel"), -1002: 222},
        history={111: [FakeMessage(1, "a", -100111), FakeMessage(2, "b", -100111)], 222: [FakeMessage(2, "c", -100222)]},
    )
    settings = Settings(telegram_channels=("@one", "@gone", -1002), telegram_api_id=1, telegram_api_hash="h")
    ingest, received, _ = ingest_with(fake, settings)
    await ingest.connect()
    assert ingest.resolved == ["@one", -1002] and ingest.connected
    # Backfill delivered oldest-first per channel, keyed by (channel, message): equal ids do not collide (FR-002).
    assert received == [(-100111, 1, "backfill"), (-100111, 2, "backfill"), (-100222, 2, "backfill")]
    assert set(fake.handlers) == {"NewMessage", "MessageEdited", "MessageDeleted"}


async def test_fr001_startup_fails_only_when_no_channel_resolves():
    fake = FakeTelegram({"@a": ValueError("x"), "@b": ValueError("y")})
    ingest, _, _ = ingest_with(fake, Settings(telegram_channels=("@a", "@b"), telegram_api_id=1, telegram_api_hash="h"))
    with pytest.raises(ChannelResolutionError, match="@a.*@b"):
        await ingest.connect()


async def test_tc110_fr006_a_long_outage_never_ends_the_listener_alerts_once_each_way_and_backfills():
    fake = FakeTelegram({"@one": 111}, history={111: [FakeMessage(1, "old", -100111)]})
    ingest, received, alerts = ingest_with(fake, Settings(telegram_channels=("@one",), telegram_api_id=1, telegram_api_hash="h"))
    await ingest.connect()
    stop = asyncio.Event()
    task = asyncio.ensure_future(ingest.run(stop))
    await asyncio.sleep(0.02)

    # Three messages are posted while the bot is disconnected; reconnecting fails many times.
    fake.history[111] += [FakeMessage(2, "m2", -100111), FakeMessage(3, "m3", -100111), FakeMessage(4, "m4", -100111)]
    fake.fail_connects = 40
    fake.drop.set()
    for _ in range(400):
        if ingest.connected and fake.connect_calls > 40:
            break
        await asyncio.sleep(0.01)
    assert not task.done()  # the process stayed up through 40 failed reconnects
    assert ingest.connected and fake.connect_calls == 42
    assert alerts == ["Telegram disconnected", "Telegram reconnected"]  # once at the start, once at recovery
    assert [(m, s) for _, m, s in received][-3:] == [(2, "backfill"), (3, "backfill"), (4, "backfill")]

    stop.set()
    await asyncio.wait_for(task, 2)


async def test_live_messages_edits_and_deletes_reach_their_callbacks():
    fake = FakeTelegram({"@one": 111})
    edits, deletes = [], []
    ingest, received, _ = ingest_with(
        fake, Settings(telegram_channels=("@one",), telegram_api_id=1, telegram_api_hash="h", telegram_backfill_limit=0),
        on_edit=lambda *a: edits.append(a), on_delete=lambda *a: deletes.append(a),
    )
    await ingest.connect()

    class Event:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    await fake.handlers["NewMessage"](Event(message=FakeMessage(9, SELL_TEXT, -100111), chat_id=-100111))
    assert received == [(-100111, 9, "live")]
    await fake.handlers["MessageEdited"](Event(message=FakeMessage(9, "edited", -100111, edit_date=START)))
    assert edits == [(-100111, 9, "edited", START.isoformat())]
    await fake.handlers["MessageDeleted"](Event(chat_id=-100111, deleted_ids=[9]))
    assert deletes == [(-100111, [9])]


async def test_session_must_be_authorised():
    from hlbot.config import ConfigError

    fake = FakeTelegram({"@one": 111})
    fake.authorized = False
    ingest, _, _ = ingest_with(fake, Settings(telegram_channels=("@one",), telegram_api_id=1, telegram_api_hash="h"))
    with pytest.raises(ConfigError, match="telegram-login"):
        await ingest.connect()


# ---- through the whole application
async def test_fr004_backfilled_messages_are_stored_once_and_expired_signals_are_marked_expired_not_traded(make):
    h = await make()
    await h.quote(4287.0)
    stale = (h.clock.now() - timedelta(minutes=45)).isoformat()
    fresh = (h.clock.now() - timedelta(minutes=5)).isoformat()
    old = h.post(SELL_TEXT, message_id=10, date=stale)  # posted 45 minutes ago, while the bot was down
    recent = h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"), message_id=11, date=fresh)
    assert h.post(SELL_TEXT, message_id=10, date=stale) is None  # the next backfill overlaps: not stored twice
    assert h.signal(old)["state"] == "EXPIRED" and not [t for t in h.trades() if t["signal_id"] == old]
    await h.settle()
    await h.until(lambda: h.signal(recent)["state"] == "TRADED", message="the still-valid backfilled signal is traded")
    row = h.db.one("SELECT text, received_at, sent_at FROM messages WHERE message_id = 10")
    assert row["text"] == SELL_TEXT and row["received_at"] and row["sent_at"] == stale  # FR-003: verbatim, with a receive time
    assert h.db.scalar("SELECT COUNT(*) FROM messages") == 2


async def test_tc111_fr005_editing_a_signal_message_creates_one_audit_event_and_one_alert_and_changes_nothing(h):
    opened = await h.open_protected_trade()
    edited = SELL_TEXT.replace("SL : 4296", "SL : 4310")
    before = (h.trade()["sl_price"], len(h.sim.requests))
    assert h.app.signals.on_edit(CHANNEL, 1477, edited)
    assert not h.app.signals.on_edit(CHANNEL, 1477, edited)  # the same edit delivered twice
    assert not h.app.signals.on_edit(CHANNEL, 1477, SELL_TEXT)  # unchanged text is not an edit
    events = h.audit("message.edited")
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload == {"old_text": SELL_TEXT, "new_text": edited}
    alerts = [a for a in h.alerts() if "was edited" in a]
    assert len(alerts) == 1 and "SL : 4296" in alerts[0] and "SL : 4310" in alerts[0]
    await h.settle()
    assert (h.trade()["sl_price"], len(h.sim.requests)) == before and h.trade()["id"] == opened["id"]  # no automatic trade change
    assert h.db.one("SELECT text, edited_at FROM messages WHERE message_id = 1477")["text"] == SELL_TEXT  # the original is preserved

    assert h.app.signals.on_delete(CHANNEL, [1477]) == 1 and h.app.signals.on_delete(None, [1477]) == 0
    assert len(h.audit("message.deleted")) == 1 and any("was deleted" in a for a in h.alerts())
    assert h.trade()["state"] == "PROTECTED"


async def test_non_signal_messages_are_stored_classified_and_never_planned(h):
    ids = [h.post("Ready ✔️⭐"), h.post("#XAUUSD ❤️\nTP 1 HIT 30+ PIPS PROFIT DONE \U0001f942"), h.post("join https://t.me/x")]
    rows = [h.signal(i) for i in ids]
    assert [r["type"] for r in rows] == ["status_ready", "tp_hit", "link"]
    assert all(r["state"] == "NOT_A_SIGNAL" for r in rows) and h.db.scalar("SELECT COUNT(*) FROM plans") == 0
    invalid = h.post(SELL_TEXT.replace("SL : 4296\n\n", ""))
    assert h.signal(invalid)["state"] == "INVALID" and "missing stop_loss" in h.signal(invalid)["reason"]  # TC-003: never guessed
    wrong_side = h.post(SELL_TEXT.replace("SL : 4296", "SL : 4280"))
    assert "stop_loss must be above entry_zone" in h.signal(wrong_side)["reason"]
    assert not h.trades() and not h.sim.requests


async def test_fr009_a_channel_format_is_a_config_change_only(make):
    h = await make(telegram_channel_parsers={"@limit_chan": "entry_limit"}, symbol_map={"GOLD": COIN, "XAUUSD": COIN})
    text = "#XAUUSD sell ENTRY 4293 Limit 4296\n\n✔️TP::::4290\n✔️TP::::4287\n✔️TP::::OPEN\n\n\U0001f6abSL:::4305"
    with_format = h.post(text, channel=-5, ref="@limit_chan")
    assert h.signal(with_format)["state"] == "WAITING_PRICE" and h.signal(with_format)["sl"] == 4305.0
    assert json.loads(h.signal(with_format)["tps"]) == {"tp1": 4290.0, "tp2": 4287.0}  # "OPEN" is left out, never guessed
    default = h.post(text, channel=-6, ref="@other_chan")
    assert h.signal(default)["state"] == "NOT_A_SIGNAL"


# ================================================================== local API and dashboard (FR-094, SR-09)
def free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_api_status_dashboard_metrics_and_token_protected_control(make):
    port = free_port()
    h = await make(api_enabled=True, api_port=port)
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")
    base = f"http://127.0.0.1:{port}"
    token = h.settings.control_token_path.read_text()
    assert h.settings.control_token_path.stat().st_mode & 0o077 == 0 and len(token) >= 32

    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base}/health") as response:
            assert response.status == 200 and (await response.json())["health"] == "HEALTHY"
            assert "Access-Control-Allow-Origin" not in response.headers
        # Nothing but the liveness check is served without a credential.
        for path in ("/api/status", "/api/price", "/api/equity", "/api/trace?trade=1", "/metrics", "/api/session", "/api/users"):
            async with http.get(f"{base}{path}") as response:
                assert response.status == 401 and "sign in" in (await response.json())["error"], path
        async with http.get(f"{base}/api/status", headers={"X-Control-Token": "wrong"}) as response:
            assert response.status == 401
        async with http.get(f"{base}/", allow_redirects=False) as response:
            assert response.status == 302 and response.headers["Location"] == "/login"
        read = {"X-Control-Token": token}
        async with http.get(f"{base}/api/status", headers=read) as response:
            status = await response.json()
        assert status["live"]["env"] == "PAPER" and status["health"]["state"] == "HEALTHY"
        assert len(status["open_trades"]) == 1 and len(status["live"]["exchange_open_orders"]) == 6
        assert status["live"]["exchange_account"]["positions"][0]["szi"] < 0 and status["live"]["exchange_account"]["age_ms"] >= 0
        assert status["live"]["quotes"][COIN]["bid"] == 4287.0 and status["live"]["stream"]["connected"]
        async with http.get(f"{base}/", headers=read) as response:
            page = await response.text()
            assert response.status == 200 and 'id="settings-dialog"' in page and "default-src 'none'" in response.headers["Content-Security-Policy"]
        async with http.get(f"{base}/login") as response:
            page = await response.text()
            assert response.status == 200 and 'id="login-form"' in page and "default-src 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["X-Frame-Options"] == "DENY" and response.headers["Cache-Control"] == "no-store"
        async with http.get(f"{base}/metrics", headers={"Authorization": f"Bearer {token}"}) as response:  # how a scraper presents it
            metrics = await response.text()
        for name in ("hlbot_quote_age_ms_xyz_GOLD", "hlbot_ws_reconnects_total", "hlbot_rest_weight_last_minute", "hlbot_time_to_protected_ms",
                     "hlbot_open_unknown_orders", "hlbot_outbox_depth", "hlbot_exchange_actions_total", "hlbot_reconcile_passes_total"):
            assert name in metrics, name  # NFR-012
        trade_id = status["open_trades"][0]["id"]
        async with http.get(f"{base}/api/trace", params={"trade": trade_id}, headers=read) as response:
            assert (await response.json())["message"]["text"] == SELL_TEXT

        # Control endpoints need the local token.
        async with http.post(f"{base}/api/control/pause", json={}) as response:
            assert response.status == 401
        async with http.post(f"{base}/api/control/pause", json={}, headers={"X-Control-Token": "wrong"}) as response:
            assert response.status == 401
        assert not h.health.operator_paused()
        headers = {"X-Control-Token": token}
        async with http.post(f"{base}/api/control/pause", json={"reason": "api test"}, headers=headers) as response:
            assert response.status == 200 and (await response.json())["state"] == "PAUSED"
        async with http.post(f"{base}/api/control/kill", json={}, headers=headers) as response:
            assert response.status == 400  # kill needs an explicit confirmation
        assert h.sim.positions[COIN][0] != 0
        async with http.post(f"{base}/api/control/kill", json={"confirm": "KILL"}, headers=headers) as response:
            assert (await response.json())["remaining"] == []
        assert h.sim.positions[COIN][0] == 0
        async with http.post(f"{base}/api/control/withdraw", json={}, headers=headers) as response:
            assert response.status == 404  # there is no such command
        # A request addressed to another host name is refused (DNS rebinding).
        async with http.get(f"{base}/api/status", headers={"Host": "evil.example", **read}) as response:
            assert response.status == 403
    assert json.loads(h.audit("control.pause")[-1]["payload"])["by"] in ("api", "operator")


async def test_inject_is_available_for_rehearsal_but_never_on_mainnet(make):
    h = await make()
    await h.quote(4287.0)
    result = await h.app.command("inject", {"text": SELL_TEXT})
    assert result["ok"] and result["state"] == "WAITING_PRICE"
    await h.settle()
    await h.until(lambda: h.trades() and h.trade()["state"] == "PROTECTED", message="PROTECTED")
    import dataclasses

    h.app.s = dataclasses.replace(h.app.s, env="mainnet")
    refused = await h.app.command("inject", {"text": SELL_TEXT})
    assert not refused["ok"] and "mainnet" in refused["error"]
    assert (await h.app.command("format-disk"))["ok"] is False


# ================================================================== service lifecycle and notifications end to end
async def test_start_and_stop_notifications_and_graceful_stop_leaves_exchange_orders_in_place(make):
    h = await make()
    await h.open_protected_trade()
    await h.until(lambda: any("Bot Started" in t for t in h.sent), message="started notification")
    open_before = sorted(o.cloid for o in h.sim.open_orders())
    (h.settings.data_path / "stop.flag").write_text("stop")  # the baseline's stop mechanism
    await h.until(lambda: h.app.stop_event.is_set(), message="stop flag noticed")
    await h.app.shutdown()
    assert any("Bot Stopped" in t and "stop flag" in t and "remain protected" in t for t in h.sent)
    assert sorted(o.cloid for o in h.sim.open_orders()) == open_before  # SL and TPs keep protecting the position
    assert not (h.settings.data_path / "stop.flag").exists() and not h.settings.pid_path.exists()
    assert not h.settings.control_token_path.exists()


async def test_five_notification_kinds_plus_alerts_are_produced_over_a_trade(h):
    await h.open_protected_trade()
    await h.quote(4296.0, 4296.1)
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
    await h.until(lambda: any("STOPPED OUT" in t for t in h.sent), message="closed notification")
    kinds = [r["kind"] for r in h.db.all("SELECT kind FROM outbox ORDER BY id")]
    assert kinds[:1] == ["service_status"] and {"info_signal", "order_placed", "signal_closed"} <= set(kinds)
    assert all("[PAPER]" in t for t in h.sent)  # FR-080: every notification is labelled


async def test_fr034_daily_target_notifies_once_per_utc_day_even_across_restart(make):
    h = await make(risk_daily_target_pct=0.1)
    await h.open_protected_trade()
    for bid in (4282.9, 4279.9, 4276.9, 4273.9, 4269.9):
        await h.quote(bid, round(bid + 0.1, 1))
    await h.until(lambda: h.trade()["state"] == "CLOSED" and h.trade()["net_pnl"] > 1.0, message="winning trade closed")
    first = h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h.quote(4287.0)
    await h.until(lambda: h.signal(first)["state"] == "REJECTED_RISK", message="rejected by the daily target")
    assert "DAILY_TARGET" in h.signal(first)["reason"]
    second = h.post(SELL_TEXT.replace("4286- 4289", "4284- 4287"))
    await h.quote(4286.0)
    await h.until(lambda: h.signal(second)["state"] == "REJECTED_RISK", message="second rejection")
    assert h.db.scalar("SELECT COUNT(*) FROM outbox WHERE kind = 'daily_target_reached'") == 1
    await h.crash()
    h2 = Harness(h.tmp_path, sim=h.sim, clock=h.clock, risk_daily_target_pct=0.1)
    h2._next_message = h._next_message
    await h2.start()
    third = h2.post(SELL_TEXT.replace("4286- 4289", "4287- 4290"))
    await h2.quote(4288.0)
    await h2.until(lambda: h2.signal(third)["state"] == "REJECTED_RISK", message="third rejection after restart")
    assert h2.db.scalar("SELECT COUNT(*) FROM outbox WHERE kind = 'daily_target_reached'") == 1  # still exactly one
    await h2.stop()


# ================================================================== TC-120: no secret in logs, audit or outbox
async def test_tc120_logs_audit_and_notifications_contain_no_secret(tmp_path):
    from hlbot.logging_setup import setup_logging
    from hlbot.redact import clear_secrets

    token = "555666777:" + "Q" * 35
    api_hash = "f00dfeedf00dfeedf00dfeedf00dfeed"
    key = "0x" + "9c" * 32
    address = "0x" + "7b" * 20
    chat_id = "-1009876543210"
    log_path = tmp_path / "logs" / "hlbot.log"
    setup_logging(log_path, "DEBUG", console=False)
    try:
        h = Harness(tmp_path, telegram_bot_token=token, telegram_api_hash=api_hash, notify_chat_id=chat_id, account_address=address)
        await h.sim.set_quote(COIN, 4300.0, 4300.1, emit=False)
        await h.start()
        await h.open_protected_trade()
        # Things that must never survive, even if some code path tried to log them.
        logging.getLogger("hlbot.test").error("send failed for https://api.telegram.org/bot%s/sendMessage", token)
        logging.getLogger("hlbot.test").info("signer", extra={"private_key": key, "account": address, "note": f"hash {api_hash} key {key}"})
        try:
            raise RuntimeError(f"boom with key {key} and token {token}")
        except RuntimeError:
            logging.getLogger("hlbot.test").exception("unexpected")
        h.app.notifier.alert("check", f"token {token} key {key}")
        h.db.audit("test.leak", {"token": token, "text": f"{api_hash} {key} {address}", "chat_id": chat_id})
        await h.quote(4296.0, 4296.1)
        await h.until(lambda: h.trade()["state"] == "CLOSED", message="CLOSED")
        dump = "\\n".join(str(row) for table in ("audit_events", "outbox", "signals", "orders", "trades", "config_versions", "kv")
                          for row in h.db.all(f"SELECT * FROM {table}"))
        await h.stop()
    finally:
        logging.getLogger().handlers.clear()
        clear_secrets()
    logs = log_path.read_text()
    assert len(logs.splitlines()) >= 8 and all(json.loads(line)["ts"] for line in logs.splitlines())  # structured JSON, one object per line
    for secret in (token, api_hash, key, key[2:], address, chat_id):
        assert secret not in logs, f"secret leaked into the log: {secret[:6]}…"
        assert secret not in dump, f"secret leaked into the database: {secret[:6]}…"
    # An account address appears only as a short prefix (NFR-022); output is ASCII-escaped (LIM-23).
    assert "0x7b7b7b\\u2026" in logs and logs.isascii()
    assert '"corr": ' in logs


# ================================================================== latency and capacity (NFR-001, NFR-002, NFR-021)
async def test_nfr002_in_range_quote_to_order_submitted_is_well_inside_250_ms(make):
    """Measures the bot's own work only (the simulated exchange answers
    instantly): network and exchange time are excluded by definition."""
    # Thirty round trips cost about 30 USDC in simulated fees: keep the daily loss limit out of the way.
    h = await make(risk_max_daily_loss_pct=20.0)
    for index in range(30):
        low = 4286 + index  # a distinct zone each time: identical content would be a duplicate
        text = SELL_TEXT.replace("4286- 4289", f"{low}- {low + 3}").replace("SL : 4296", f"SL : {low + 10}")
        text = text.replace("TP1 : 4283", f"TP1 : {low - 3}")
        signal_id = h.post(text, channel=-(2000 + index), ref=f"@c{index}")
        await h.quote(low + 1.0)
        await h.until(lambda sid=signal_id: h.signal(sid)["state"] == "TRADED", message=f"trade {index}")
        await h.app.command("close", {"trade_id": h.trade()["id"]})
        await h.until(lambda: h.trade()["state"] == "CLOSED", message=f"closed {index}")
        h.clock.advance(60, monotonic=False)
        h.sim.touch()
    metrics = h.app.metrics
    assert metrics.snapshot()["decision_to_submit_ms_count"] == 30
    assert metrics.quantile("decision_to_submit_ms", 0.95) < 250.0
    assert metrics.quantile("signal_to_intent_ms", 0.95) < 3000.0  # NFR-001, price already in range
    assert metrics.quantile("time_to_protected_ms", 0.95) < h.settings.protect_soft_ms


async def test_nfr021_ten_times_the_expected_daily_message_volume_is_ingested_quickly(h):
    """Capacity: 200 messages a day are expected; 2,000 mixed messages
    (ten times that) must not need any rework. Each is one durable
    transaction."""
    import time

    await h.quote(4300.0)
    texts = ["Ready ✔️⭐", "#XAUUSD ❤️\nTP 1 HIT 30+ PIPS PROFIT DONE", "promo: contact admin https://t.me/x", SELL_TEXT.replace("SL : 4296", "SL : 4280")]
    started = time.perf_counter()
    for index in range(2000):
        assert h.post(texts[index % 4], channel=-(index % 10 + 1), message_id=index + 1, ref=f"@chan{index % 10}") is not None
    elapsed = time.perf_counter() - started
    assert h.db.scalar("SELECT COUNT(*) FROM messages") == 2000
    assert h.db.scalar("SELECT COUNT(DISTINCT channel_id) FROM messages") == 10
    assert elapsed / 2000 < 0.05, f"{elapsed / 2000 * 1000:.1f} ms per message"
    tick = time.perf_counter()
    await h.app.signals.tick()
    assert time.perf_counter() - tick < 0.5
    assert not h.trades() and h.health.state() == "HEALTHY"


# ================================================================== Telethon client construction (SR-04)
def test_sr04_session_file_readable_by_others_is_refused_and_a_new_one_gets_a_private_directory(tmp_path):
    from hlbot.config import ConfigError, SecretFileError
    from hlbot.ingest.telegram import build_client, format_message

    session = tmp_path / "tg" / "bot.session"
    settings = Settings(telegram_api_id=12345, telegram_api_hash="0" * 32, telegram_session=str(session), telegram_channels=("@c",))
    client = build_client(settings)  # constructs only; nothing connects
    assert client.session.filename == str(session)
    assert session.parent.stat().st_mode & 0o077 == 0
    assert session.stat().st_mode & 0o077 == 0  # created owner-only, so the next start accepts it
    client.session.close()
    session.chmod(0o644)
    with pytest.raises(SecretFileError, match="Telegram session file"):
        build_client(settings)
    session.chmod(0o600)
    with pytest.raises(ConfigError, match="TELEGRAM_API_ID"):
        build_client(Settings(telegram_session=str(session)))

    class Reply:
        reply_to_msg_id = 7

    class Message:
        id, chat_id, message, media, reply_to = 9, -100123, "raw  text\n", object(), Reply()
        date = START

    formatted = format_message(Message(), "@c")
    assert formatted == {"message_id": 9, "channel_id": -100123, "channel_ref": "@c", "date": START.isoformat(),
                         "text": "raw  text\n", "has_media": True, "reply_to": 7}


def test_read_only_open_works_on_a_stopped_wal_database_and_still_refuses_writes(tmp_path):
    """What ``botctl status`` does while the bot is down."""
    db = fresh_db(tmp_path)
    db.kv_set("a", 1)
    db.close()  # clean shutdown: the -wal and -shm files are gone
    for companion in ("t.sqlite3-wal", "t.sqlite3-shm"):
        (tmp_path / companion).unlink(missing_ok=True)
    reader = Database(tmp_path / "t.sqlite3", readonly=True)
    assert reader.kv_get("a") == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO kv (key, value, updated_at) VALUES ('b', '1', 'now')")
    reader.close()
    with pytest.raises(Exception, match="not found"):
        Database(tmp_path / "missing.sqlite3", readonly=True)


# ================================================================== dashboard read models and page (FR-094)
def test_equity_history_filters_by_range_and_dex_and_thins_without_losing_the_latest_point(tmp_path):
    from hlbot.api import readmodel

    clock = ManualClock(START)
    db = fresh_db(tmp_path, clock)
    for minutes_ago in range(600, -1, -1):  # ten hours, one point a minute, two dexes
        at = (START - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
        db.insert("account_snapshots", env="testnet", dex="", account_value=200.0 + (600 - minutes_ago) * 0.01, snapshot_at=at)
        db.insert("account_snapshots", env="testnet", dex="par", account_value=1000.0, snapshot_at=at)

    hour = readmodel.equity_history(db, clock, "1h", [""])["points"]
    assert len(hour) == 61 and hour[0]["value"] == pytest.approx(205.4) and hour[-1]["value"] == pytest.approx(206.0)
    day = readmodel.equity_history(db, clock, "1d", [""])
    assert day["range"] == "1d" and len(day["points"]) == 240  # thinned from 601
    # the response names the window that was asked for, so the chart's axis can span it
    assert day["to"] == START.isoformat(timespec="milliseconds") and day["from"] == (START - timedelta(days=1)).isoformat(timespec="milliseconds")
    week, empty = readmodel.equity_history(db, clock, "1w", [""]), readmodel.equity_history(db, clock, "1h", [])
    assert week["from"] == (START - timedelta(days=7)).isoformat(timespec="milliseconds") and empty["from"] and empty["to"]
    assert len(week["points"]) == len(day["points"])  # same ten hours of data; only the window differs
    assert day["points"][-1]["at"] == START.isoformat(timespec="milliseconds")  # the latest point always survives
    assert [p["at"] for p in day["points"]] == sorted(p["at"] for p in day["points"])
    assert readmodel.equity_history(db, clock, "1h", ["", "par"])["points"][-1]["value"] == pytest.approx(1206.0)  # dexes add up
    assert readmodel.equity_history(db, clock, "1h", [])["points"] == []
    assert readmodel.equity_history(db, clock, "1h", ["xyz"])["points"] == []
    with pytest.raises(ValueError, match="1h, 1d, 1w"):
        readmodel.equity_history(db, clock, "5y", [""])
    db.close()


async def closed_win_and_loss(h):
    await h.open_protected_trade()
    for bid in (4282.9, 4279.9, 4276.9, 4273.9, 4269.9):
        await h.quote(bid, round(bid + 0.1, 1))
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="win closed")
    h.post(SELL_TEXT.replace("4286- 4289", "4285- 4288"))
    await h.quote(4287.0)
    await h.until(lambda: h.trade()["state"] == "PROTECTED" and h.trade()["id"] == 2, message="second trade open")
    await h.quote(4296.0, 4296.1)
    await h.until(lambda: h.trade()["state"] == "CLOSED", message="loss closed")


async def test_trade_stats_and_equity_endpoint_feed_the_dashboard(make):
    from hlbot.api import readmodel

    port = free_port()
    h = await make(api_enabled=True, api_port=port)
    empty = readmodel.trade_stats(h.db, clock=h.clock)
    assert empty["closed"] == 0 and empty["win_rate"] is None and empty["recent_closed"] == []
    assert empty["today"] == {"date": h.clock.utc_date(), "closed": 0, "wins": 0, "losses": 0, "win_rate": None}
    await closed_win_and_loss(h)
    stats = readmodel.trade_stats(h.db)
    assert (stats["closed"], stats["gain_count"], stats["loss_count"], stats["win_rate"]) == (2, 1, 1, 0.5)
    assert stats["gain_total"] > 0 > stats["loss_total"] and stats["net_total"] == pytest.approx(stats["gain_total"] + stats["loss_total"])
    assert stats["by_outcome"] == {"LOSS_ALL": 1, "WIN_ALL": 1}
    assert [t["id"] for t in stats["recent_closed"]] == [1, 2]  # oldest first, as the chart reads
    # the daily win rate counts only what closed on the current UTC day
    today = readmodel.trade_stats(h.db, clock=h.clock)["today"]
    assert today == {"date": h.clock.utc_date(), "closed": 2, "wins": 1, "losses": 1, "win_rate": 0.5}
    h.db.execute("UPDATE trades SET closed_at = ? WHERE id = 2", ((h.clock.now() - timedelta(days=1)).isoformat(),))
    won = h.db.scalar("SELECT net_pnl FROM trades WHERE id = 1") > 0
    today = readmodel.trade_stats(h.db, clock=h.clock)["today"]
    assert (today["closed"], today["wins"], today["losses"], today["win_rate"]) == (1, int(won), int(not won), 1.0 if won else 0.0)
    h.db.execute("UPDATE trades SET closed_at = ? WHERE id = 1", ((h.clock.now() - timedelta(days=1)).isoformat(),))
    assert readmodel.trade_stats(h.db, clock=h.clock)["today"]["win_rate"] is None  # nothing closed today: no rate, not 0 %
    await h.app.reconciler.run_once("test")
    headers = {"X-Control-Token": h.settings.control_token_path.read_text()}
    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.get(f"http://127.0.0.1:{port}/api/status") as response:
            assert (await response.json())["stats"]["closed"] == 2
        async with http.get(f"http://127.0.0.1:{port}/api/equity", params={"range": "1h"}) as response:
            data = await response.json()
            assert response.status == 200 and data["range"] == "1h" and len(data["points"]) >= 1 and data["points"][-1]["value"] > 0
        async with http.get(f"http://127.0.0.1:{port}/api/equity", params={"range": "forever"}) as response:
            assert response.status == 400 and "1h, 1d, 1w" in (await response.json())["error"]


_DOM_STUB = r"""
const fs = require("fs");
const [htmlPath, statusPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.split("<script>").pop().split("</script>")[0];  // the page script; the one in <head> only applies a saved theme
const text = (h) => String(h).replace(/<[^>]*>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
function element() {
  const e = {_html: "", textContent: "", className: "", style: {}, dataset: {}, attrs: {}, clientWidth: 640, offsetWidth: 120, offsetHeight: 40, children: [],
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k]; },
    addEventListener() {}, querySelector() { return element(); }, querySelectorAll() { return []; },
    append(...nodes) { this.children.push(...nodes); }, appendChild(node) { this.children.push(node); return node; },
    getBoundingClientRect() { return {left: 0, top: 0, width: 640, height: 230}; },
    classList: {toggle() {}}};
  Object.defineProperty(e, "innerHTML", {get() { return this._html; }, set(v) { this._html = String(v); this.textContent = text(v); }});
  return e;
}
const ids = {};
for (const id of html.match(/id="([\w-]+)"/g).map((m) => m.slice(4, -1))) ids[id] = element();
const stored = {};
global.localStorage = {getItem: (k) => (k in stored ? stored[k] : null), setItem(k, v) { stored[k] = String(v); }, removeItem(k) { delete stored[k]; }};
global.location = {hash: "", assign() {}, replace() {}};
global.CSS = {escape: (v) => String(v)};
global.document = {documentElement: {dataset: {}, scrollHeight: 2000}, title: "", addEventListener() {}, getElementById: (id) => { if (!ids[id]) throw new Error("unknown element id: " + id); return ids[id]; },
  createElement: () => element(), createTextNode: (t) => ({textContent: String(t)}), querySelectorAll: () => [], querySelector: () => null};
global.window = {innerWidth: 1440, innerHeight: 900, scrollY: 0, addEventListener() {}, confirm: () => false, prompt: () => null, alert() {}};
global.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
global.setInterval = () => 0;
global.fetch = async () => ({ok: false, status: 503});
const api = new Function(script + "\nreturn {render, renderEquity, money, setTheme, currentTheme, setFolded, folded, applyName, toggleTrade, renderHistory, renderAnalytics, compact,"
  + " setVolume(v, problem) { lastVolume = v; volumeProblem = problem || null; }, setPeriod(p) { analyticsPeriod = p; }};")();
const status = JSON.parse(fs.readFileSync(statusPath, "utf8"));
const page = (trades) => ({total: trades.length, page: 1, pages: 1, page_size: 20});
api.render(status.full);
const loadingMarket = ids["market"]._html;                      // before any volume has arrived
api.setVolume(null, "volume not available: HTTP 502"); api.render(status.full);
const failedMarket = ids["market"]._html;
api.setVolume(status.volume); api.render(status.full);
api.renderEquity(status.equity);
api.renderHistory(status.full.recent_trades, page(status.full.recent_trades));
api.renderAnalytics(status.analytics);
const out = {};
for (const [id, e] of Object.entries(ids)) out[id] = {html: e._html, text: e.textContent, cls: e.className, aria: e.attrs["aria-label"] || ""};
out.loadingMarket = loadingMarket; out.failedMarket = failedMarket;
out.compact = [api.compact(1269840.7, 2), api.compact(47400), api.compact(0.57396, 2), api.compact(15.40545, 2), api.compact(null)];
api.setPeriod("month"); api.renderAnalytics(status.analytics); out.monthBars = ids["an-periods"]._html;
api.renderAnalytics(status.analyticsEmpty); out.emptyAnalytics = {state: ids["an-state"].textContent, hidden: ids["an-state"].hidden, kpis: ids["an-kpis"].textContent,
  cumulative: ids["an-cumulative"]._html, bars: ids["an-periods"]._html, table: ids["an-symbols-table"]._html};
api.toggleTrade(1); api.renderHistory(status.full.recent_trades, page(status.full.recent_trades)); out.historyOpen = ids["history-table"]._html; api.toggleTrade(1);
api.renderHistory([{id: 9, coin: "<b>X</b>", side: "BUY", state: "CLOSED", outcome: "FLATTENED", planned_size: 0.02, filled_size: 0.02, sl_price: 4202,
  tp_plan: JSON.stringify([{level: 1, price: 4216, size: 0.01}, {level: 2, price: 4221, size: 0.01}]), close_reason: "<i>kill</i>",
  orders: [{id: 1, role: "TP", leg: 1, attempt: 2, side: "SELL", type: "limit", tif: "Gtc", price: 4216, size: 0.01, filled_size: 0, state: "CANCELED", exchange_status: "reduceOnlyCanceled"},
           {id: 2, role: "ENTRY", leg: 0, attempt: 1, side: "BUY", type: "limit", tif: "Ioc", price: 4214.8, size: 0.02, filled_size: 0.02, avg_fill_price: 4213.9, state: "FILLED", exchange_status: "filled"},
           {id: 3, role: "CLOSE", leg: 0, attempt: 1, side: "SELL", type: "limit", tif: "Ioc", price: 4170.6, size: 0.02, filled_size: 0.02, avg_fill_price: 4211.1, state: "REJECTED", error_text: "<u>too far</u>"}]}],
  {total: 41, page: 2, pages: 3, page_size: 20});
out.historyPlanned = ids["history-table"]._html; out.historyPlannedText = ids["history-table"].textContent;
out.paged = [ids["history-sub"].textContent, ids["hi-count"].textContent, ids["hi-page"].textContent, ids["hi-prev"].disabled, ids["hi-next"].disabled];
api.renderHistory([], {total: 0, page: 1, pages: 1, page_size: 20, filtered: true}); out.noMatch = ids["history-table"]._html;
api.render(status.full);
out.money = [api.money(0), api.money(0.001), api.money(-0.004), api.money(1.5), api.money(-2), api.money(null)];
api.render(status.empty);
api.renderEquity({range: "1d", points: []});
out.emptyConnections = ids["connections"]._html; out.emptyTrades = ids["trades"]._html; out.emptyLine = ids["line"]._html; out.emptyBars = ids["bars"]._html; out.emptyHealth = ids["health"].textContent;
out.themes = [api.currentTheme(), ids["theme-toggle"].attrs["aria-label"]];
api.setTheme("light"); out.themes.push(api.currentTheme(), ids["theme-toggle"].attrs["aria-label"], stored["hlbot-theme"]);
api.setTheme("sepia"); out.themes.push(api.currentTheme(), stored["hlbot-theme"]);
out.folds = [{...api.folded}, ids["signals-toggle"].attrs["aria-expanded"], ids["positions-toggle"].attrs["aria-expanded"], ids["history-body"].inert];
api.setFolded("history", false); out.folds.push(ids["history-toggle"].attrs["aria-expanded"], ids["history-body"].inert, stored["hlbot-folded"]);
api.applyName("<b>Desk</b>"); out.named = [ids["brand-name"].textContent, ids["brand-name"]._html, document.title, ids["name-input"].value];
api.applyName(""); out.named.push(ids["brand-name"].textContent, ids["name-input"].value);
process.stdout.write(JSON.stringify(out));
"""


async def test_dashboard_script_renders_a_real_status_payload_and_escapes_what_it_shows(make, tmp_path):
    """Runs the page's own JavaScript under Node against data from a
    live application. Skipped where Node is not installed."""
    import shutil
    import subprocess
    from importlib import resources

    from hlbot.api import readmodel
    from hlbot.api.server import ApiServer

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    h = await make()
    await closed_win_and_loss(h)
    await h.open_protected_trade(SELL_TEXT.replace("4286- 4289", "4284- 4287"), bid=4286.0)
    await h.app.reconciler.run_once("test")
    h.post(SELL_TEXT.replace("4286- 4289", "4310- 4313").replace("SL : 4296", "SL : 4320"))
    h.post(SELL_TEXT.replace("SL : 4296", "SL : 4280"))  # invalid
    h.app.notifier.alert("<script>alert(1)</script> injected", "detail & <b>more</b>", critical=True)
    await h.app.command("pause", {"reason": "<img src=x onerror=1>"})
    full = readmodel.build_status(h.db, h.clock)
    # each trade in the history carries its own orders, in the order a trade lives through them, without signing details
    for trade in full["recent_trades"]:
        roles = [(o["role"], o["leg"]) for o in trade["orders"]]
        assert roles == sorted(roles, key=lambda r: ({"ENTRY": 0, "SL": 1, "TP": 2, "CLOSE": 3}[r[0]], r[1])), roles
        assert roles[0] == ("ENTRY", 0) and ("SL", 0) in roles and [r for r in roles if r[0] == "TP"] == [("TP", leg) for leg in range(1, 6)]
        assert not {"cloid", "nonce", "oid", "trade_id"} & set(trade["orders"][0])
    assert readmodel.recent_trades(Database(h.settings.db_path, readonly=True), 0) == []
    full["live"] = ApiServer(h.app)._live()
    assert full["live"]["latency"] == {"rest_ms": None, "rest_avg_ms": None, "ws_ping_ms": None}  # simulated venue: no network
    full["live"]["latency"] = {"rest_ms": 123.4, "rest_avg_ms": 640.0, "ws_ping_ms": 2100.0}
    equity = readmodel.equity_history(h.db, h.clock, "1d", ["xyz"])
    equity["points"] += [{"at": (h.clock.now() + timedelta(minutes=i)).isoformat(), "value": 1000.0 + i} for i in range(1, 6)]
    equity["to"] = equity["points"][-1]["at"]
    assert equity["range"] == "1d" and equity["from"] < equity["points"][0]["at"]
    empty = {"health": {"state": "HEALTHY", "control": {"mode": "RUNNING"}, "conditions": []}, "day": {}, "stats": {}, "live": {"quotes": {}},
             "open_trades": [], "waiting_signals": [], "recent_trades": [], "recent_signals": [], "alerts": [], "outbox_pending": 0}
    payload = tmp_path / "status.json"
    # what the volume and analytics endpoints would send for this history, with one hostile name to prove it is shown as text
    from hlbot.api import analytics as analytics_module
    from hlbot.api.market import volume_summary
    from tests.component.test_analytics import candles

    now_ms = h.clock.now_ms()
    volume = {"interval": "1h", "symbols": {COIN: {**volume_summary(candles([10.0] * 19 + [30.0, 8.0], end_ms=now_ms), now_ms, "1h"), "base_unit": "GOLD",
                                                   "day": {}, "age_ms": 1200, "stale": True, "error": "<b>upstream</b> timeout"}}}
    full["live"]["quotes"][COIN].update(day_volume_base=15.40545, day_volume_quote=1269840.7, prev_day_price=4200.0)
    figures = analytics_module.analytics(h.db, analytics_module.TradeFilter(tz_minutes=420), h.clock, ["xyz"])
    assert figures["kpis"]["total_trades"] == 2 and figures["kpis"]["wins"] == 1 and len(figures["cumulative"]) == 2
    figures["by_source"].append({"key": "<img src=x onerror=1>", "trades": 1, "wins": 0, "losses": 1, "win_rate": 0.0, "net": -1.5, "fees": 0.1, "volume": 12.0})
    nothing = analytics_module.analytics(h.db, analytics_module.TradeFilter(symbols=("NOPE",)), h.clock, [])
    payload.write_text(json.dumps({"full": full, "equity": equity, "empty": empty, "volume": volume, "analytics": figures, "analyticsEmpty": nothing}, default=str))
    stub = tmp_path / "stub.js"
    stub.write_text(_DOM_STUB)
    page = resources.files("hlbot.api").joinpath("dashboard.html")
    result = await asyncio.to_thread(
        subprocess.run, [node, str(stub), str(page), str(payload)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout)

    assert out["env"]["text"] == "PAPER" and out["health"]["text"] == "PAUSED" and "warn" in out["health"]["cls"]
    assert out["health-top"]["text"] == "PAUSED" and out["health-text"]["text"] == "Paused"
    assert "PROTECTED" in out["trades"]["html"] and out["trades"]["html"].count('class="row') == 1
    assert out["orders-table"]["html"].count("<tr>") == 7  # header + stop + five take-profits
    assert "GOLD-SELL-4310-4313" in out["waiting"]["html"] and "INVALID" in out["recent-signals"]["html"]
    assert "WIN_ALL" in out["history-table"]["html"] and "LOSS_ALL" in out["history-table"]["html"]
    # history: every trade is a row that opens to its orders; all start closed, and an opened one survives a redraw
    closed, opened = out["history-table"]["html"], out["historyOpen"]
    assert closed.count('class="trade-row"') == 3 and closed.count('class="trade-detail"') == closed.count(" hidden>") == 3
    assert closed.count('aria-expanded="false"') == 3 and 'aria-controls="trade-detail-1"' in closed
    assert opened.count('class="trade-row open"') == 1 and opened.count(" hidden>") == 2 and opened.count('aria-expanded="true"') == 1
    first = opened.split('id="trade-detail-1"')[1].split('<tr class="trade-row')[0]
    names = [cell.split("</b>")[0] for cell in first.split("<td class=\"\"><b>")[1:]]
    assert names == ["Entry", "Stop loss", "TP1", "TP2", "TP3", "TP4", "TP5"], names  # the life of the trade, in order
    assert "Stop market" in first and "Limit · IOC" in first and first.count("FILLED") >= 2 and "Opened" in first and "Realised" in first
    # an order that never reached the exchange is still listed from the plan; nothing from the data becomes markup
    planned = out["historyPlanned"]
    names = [cell.split("</b>")[0] for cell in planned.split("<td class=\"\"><b>")[1:]]
    assert names == ["Entry", "Stop loss", "TP1 · attempt 2", "TP2", "Close"], names
    assert planned.count("NOT_PLACED") == 2 and "removed by the exchange when the position closed" in planned
    assert "<i>kill</i>" not in planned and "<u>too far</u>" not in planned and "<b>X</b>" not in planned
    assert "<i>kill</i>" in out["historyPlannedText"] and "<u>too far</u>" in out["historyPlannedText"]
    # gains and losses, each with its sign; one bar per closed trade, in both colours
    assert out["gain-total"]["text"].startswith("+$") and out["loss-total"]["text"].startswith("−$")
    assert "2 closed · win rate 50%" in out["stats-sub"]["text"]
    assert out["bars"]["html"].count('class="bar pos"') == 1 and out["bars"]["html"].count('class="bar neg"') == 1
    # charts carry no colour of their own: both themes paint them from the stylesheet
    assert "#" not in out["bars"]["html"].replace("&#", "") and 'class="c-line"' in out["line"]["html"]
    # dark unless chosen; the choice is remembered; anything that is not "light" is dark
    assert out["themes"] == ["dark", "Switch to light theme", "light", "Switch to dark theme", "light", "dark", "dark"]
    # positions and orders start open, the two long lists start folded; a change is remembered
    assert out["folds"][:4] == [{"outlook": False, "analytics": False, "positions": False, "orders": False, "signals": True, "history": True}, "false", "true", True]
    assert out["folds"][4:6] == ["true", False] and json.loads(out["folds"][6])["history"] is False
    # the dashboard name is written as text, never as markup; empty means the default
    assert out["named"] == ["<b>Desk</b>", "", "<b>Desk</b> dashboard", "<b>Desk</b>", "hlbot", ""]
    # daily win rate box, and the counts that stay visible beside a folded heading
    assert out["day-winrate"]["text"] == "50%" and out["day-winrate-sub"]["text"] == "1 won · 1 lost today (UTC)"
    assert out["signals-sub"]["text"] == "1 waiting · 5 recent" and out["orders-sub"]["text"].startswith("6 on the exchange")
    assert out["history-sub"]["text"] == "3 trades" and out["hi-count"]["text"] == "Showing 1 to 3 of 3"
    # a later page of a filtered history says where it is; no match says so
    assert out["paged"] == ["41 trades · page 2 of 3", "Showing 21 to 21 of 41", "Page 2 of 3", False, False] and "No trades match these filters" in out["noMatch"]

    # ---- market card: volume in three kinds, with its loading, failed and stale states
    assert 'class="full skeleton"' in out["loadingMarket"] and "24h volume" in out["loadingMarket"]
    assert "volume not available: HTTP 502" in out["failedMarket"] and "data-volume-retry" in out["failedMarket"]
    market = out["market"]["html"]
    assert "Now · 1h candle" in market and "8 GOLD" in market and "70 trades" in market and "50% through" in market       # base and tick volume of the forming candle
    assert "Average of 20" in market and "11 GOLD" in market and "Last closed candle" in market and "30 GOLD" in market
    assert "+173% vs average" in market and "unusually high" in market and "rising against the candle before (+200%)" in market
    assert "15.41 GOLD" in market and "$1.27M quote volume" in market and "≈ $" in market                                  # exact 24h quote volume; a candle's is an estimate
    assert 'class="trend stale"' in market and "&lt;b&gt;upstream&lt;/b&gt; timeout" in market and "<b>upstream</b>" not in market
    assert "24h</span>" in market and ("+2.0" in market or "+2.1" in market)                                                # against the price 24 hours ago
    assert out["compact"] == ["1.27M", "47.4K", "0.574", "15.41", "–"]  # under 1, more digits are kept

    # ---- analytics: key figures, charts and tables from the real history
    kpis = out["an-kpis"]["html"]
    assert kpis.count('class="kpi"') == 12 and kpis.count('class="info"') == 12 and kpis.count("data-tip=") == 12
    for label in ("Total trades", "Win rate", "Net P/L", "Gross profit", "Gross loss", "Profit factor", "Average win", "Average loss", "Avg planned R:R", "Max drawdown", "Fees",
                  "Account equity"):
        assert f"<span>{label}</span>" in kpis, label
    assert "50%" in out["an-kpis"]["text"] and "1 won · 1 lost" in out["an-kpis"]["text"] and "1 still open, not counted" in out["an-kpis"]["text"]
    assert out["an-state"]["text"] == "" and "2 closed trades" in out["analytics-sub"]["text"] and out["an-zone"]["text"] == "Days and dates in UTC+07:00"
    assert 'class="c-a"' in out["an-cumulative"]["html"] and "Cumulative net profit and loss" in out["an-cumulative"]["html"]
    assert 'class="c-a"' in out["an-equity"]["html"] and 'class="endlabel"' in out["an-equity"]["html"]
    assert out["an-periods"]["html"].count('class="bar ') >= 1 and out["monthBars"].count('class="bar ') == 1                # one month, however many days
    assert 'class="bar neutral"' in out["an-activity"]["html"] and "#" not in out["an-periods"]["html"].replace("&#", "")
    assert out["an-distribution"]["html"].count('class="bar pos"') >= 1 and out["an-distribution"]["html"].count('class="bar neg"') >= 1
    assert "WIN_ALL 1" in out["an-outcomes"]["text"] and "LOSS_ALL 1" in out["an-outcomes"]["text"]
    assert "xyz:GOLD" in out["an-symbols-table"]["html"] and "1 / 1" in out["an-symbols-table"]["html"]
    assert "&lt;img src=x onerror=1&gt;" in out["an-sources-table"]["html"] and "<img src=x" not in out["an-sources-table"]["html"]
    assert "BUY" in out["an-sources-table"]["text"] or "SELL" in out["an-sources-table"]["text"]
    empty = out["emptyAnalytics"]
    assert empty["hidden"] is False and "No closed trades" in empty["state"] and "–" in empty["kpis"] and "No data for this selection" in empty["cumulative"]
    assert "No data for this selection" in empty["bars"] and "No closed trades in this selection" in empty["table"]
    # alerts: the side column links into the pop-up, where each alert opens in place
    assert out["alert-rows"]["html"].count("<button") == out["alert-rows"]["html"].count("data-alert=") <= 3
    assert out["alerts-list"]["html"].count("<details") == 1 and "detail &amp; &lt;b&gt;more&lt;/b&gt;" in out["alerts-list"]["html"]
    assert "Telegram: " in out["alerts-list"]["text"] and out["alerts-count"]["text"].startswith("latest ")
    assert out["ring"]["aria"].startswith("margin used: ") and out["ring"]["aria"].endswith("%")
    # the area chart: a 2px line, a labelled endpoint, an accessible name, and a table view of the same data
    assert 'stroke-width="2"' in out["line"]["html"] and "Account value, last 24 hours" in out["line"]["html"] and 'class="endlabel"' in out["line"]["html"]
    # the time axis spans the selected 24 hours (a tick every 6 hours), and the note admits the history is shorter
    assert out["line"]["html"].count('y2="278"') >= 3 and "Last 24 hours" in out["range-note"]["text"] and "history starts" in out["range-note"]["text"]
    first_x = float(out["line"]["html"].split('<path d="M')[1].split(" ")[0])
    assert first_x > 400, "with a few minutes of data in a 24-hour window the line belongs at the right-hand end"
    assert "$1,005.00" in out["line"]["html"] and "$1,005.00" in out["line-table"]["html"]
    # connection latency, each with a dot that matches the number
    connections = out["connections"]["html"]
    assert 'API latency</dt><dd><span class="dot"></span>123 ms · avg 640 ms' in connections
    assert 'Stream ping</dt><dd><span class="dot off"></span>2,100 ms' in connections
    # nothing from the data is ever written as markup
    everything = "".join(v["html"] for v in out.values() if isinstance(v, dict) and "html" in v)
    assert "<script>alert(1)" not in everything and "<img src=x" not in everything and "<b>more</b>" not in everything
    assert "alert(1)" in out["alert-rows"]["text"] and "onerror" in out["conditions"]["text"]
    # a zero carries no sign; tiny values that round to zero do not either
    assert out["money"] == ["$0.00", "$0.00", "$0.00", "+$1.50", "−$2.00", "–"]
    # an empty system still renders
    assert "No open trades" in out["emptyTrades"] and "No history in this range" in out["emptyLine"] and "No closed trades" in out["emptyBars"]
    assert out["emptyHealth"] == "HEALTHY"
    assert out["emptyConnections"].count("not measured") == 2


def test_dashboard_uses_the_theme_tokens_and_stays_self_contained():
    """The palette from the design brief is present, and the page loads
    nothing from another origin (the CSP forbids it)."""
    import re
    from importlib import resources

    page = resources.files("hlbot.api").joinpath("dashboard.html").read_text(encoding="utf-8")
    for token in ("--bg: #141414", "--surface: #1b1b1b", "--surface-deep: #0b0b0b", "--border: #2a2a2a", "--lime: #c6eb72", "--yellow: #e8d94c",
                  "--teal: #4dd9c6", "--danger: #ff5c5c", "--link: #3a9bff", "--radius-card: 20px", "--radius-badge: 5px",
                  "linear-gradient(135deg, var(--teal), var(--lime), var(--yellow))", "linear-gradient(135deg, #2a2e45, #4a3f73)", '"Plus Jakarta Sans", "Inter"'):
        assert token in page, token
    assert not re.search(r'(?:src|href)="https?://', page) and "@import" not in page and "url(http" not in page
    # two inline scripts: one line in <head> that applies a saved theme before first paint, and the page script
    assert page.count("<script") == 2 and "innerHTML = d." not in page
    assert '<img class="logo" id="logo" src="/logo"' in page and '<link rel="icon" href="/logo">' in page


def test_dashboard_light_theme_overrides_every_surface_and_keeps_chart_colours_out_of_the_script():
    """Light mode is a second set of the same tokens, and the charts are
    painted from the stylesheet, so a hard-coded dark colour cannot be
    left behind in the script."""
    import re
    from importlib import resources

    page = resources.files("hlbot.api").joinpath("dashboard.html").read_text(encoding="utf-8")
    light = page.split(':root[data-theme="light"] {')[1].split("}")[0]
    for token in ("--bg:", "--surface:", "--surface-deep:", "--border:", "--text:", "--text-muted:", "--link:", "--pos:", "--neg:", "--pos-text:",
                  "--neg-text:", "--chart-line:", "--area-top:", "--area-mid:", "--ring-a:", "--nav-active:", "--focus:", "--shadow:", "color-scheme: light"):
        assert token in light, token
    assert "--pos: #0e8c7c" in light and "--neg: #d93838" in light  # the pair checked for colour-blind separation on white
    script = page.split("<script>")[-1]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", script), "colours belong in the stylesheet tokens"
    assert 'data-theme-choice="dark"' in page and 'data-theme-choice="light"' in page
    # the theme switch is an icon with a spoken name, settings and alerts are pop-ups, and motion can be turned off
    assert 'id="theme-toggle" class="iconbtn" aria-label=' in page and 'class="themeicon"' in page and "prefers-reduced-motion: reduce" in page
    assert '<dialog id="settings-dialog"' in page and '<dialog id="alerts-dialog"' in page and 'id="nav-settings"' in page and 'id="nav-alerts"' in page
    assert 'href="#settings"' not in page and 'href="#alerts"' not in page  # only real sections are menu anchors
    for section in ("positions", "orders", "signals", "history"):
        assert f'<section id="{section}" class="fold">' in page and f'id="{section}-toggle" aria-expanded="true" aria-controls="{section}-body"' in page
    # no credential is ever kept by the page: it rides on the HttpOnly session cookie
    assert "X-Control-Token" not in page and "sessionStorage" not in page and "controlToken" not in page
    for control in ('id="pause" data-need="operator"', 'id="resume" class="primary" data-need="operator"', 'id="kill" class="danger" data-need="operator"',
                    'id="name-form" data-need="admin"', 'data-tab="users" aria-controls="panel-users" aria-selected="false" data-need="admin"'):
        assert control in page, control


async def test_logo_default_upload_validation_and_reset(make):
    """The packaged logo is served by default; a replacement needs the
    control token, must really be an image, and can be removed again."""
    from importlib import resources

    port = free_port()
    h = await make(api_enabled=True, api_port=port)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Control-Token": h.settings.control_token_path.read_text()}
    packaged = resources.files("hlbot.api").joinpath("logo.png").read_bytes()
    custom = h.settings.data_path / "ui-logo"
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    assert packaged.startswith(b"\x89PNG\r\n\x1a\n")

    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base}/logo") as response:
            assert response.status == 200 and response.content_type == "image/png" and await response.read() == packaged
            assert response.headers["Cache-Control"] == "no-store" and response.headers["X-Content-Type-Options"] == "nosniff"
        async with http.get(f"{base}/login") as response:
            assert "img-src 'self'" in response.headers["Content-Security-Policy"]

        # changing it is an operator action
        async with http.post(f"{base}/api/settings/logo", data=jpeg) as response:
            assert response.status == 401
        async with http.post(f"{base}/api/settings/logo", data=jpeg, headers={"X-Control-Token": "wrong"}) as response:
            assert response.status == 401
        async with http.delete(f"{base}/api/settings/logo") as response:
            assert response.status == 401
        assert not custom.exists()

        # the bytes decide, not the name or the declared type
        for body in (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"<html><script>alert(1)</script>", b"", b"GIF"):
            async with http.post(f"{base}/api/settings/logo", data=body, headers={**headers, "Content-Type": "image/png"}) as response:
                assert response.status == 400, body
        async with http.post(f"{base}/api/settings/logo", data=b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024), headers=headers) as response:
            assert response.status == 413
        assert not custom.exists()

        async with http.post(f"{base}/api/settings/logo", data=jpeg, headers={**headers, "Content-Type": "text/html"}) as response:
            assert response.status == 200 and (await response.json())["type"] == "image/jpeg"
        assert custom.read_bytes() == jpeg and custom.stat().st_mode & 0o077 == 0
        async with http.get(f"{base}/logo") as response:
            assert response.content_type == "image/jpeg" and await response.read() == jpeg

        async with http.delete(f"{base}/api/settings/logo", headers=headers) as response:
            assert response.status == 200 and (await response.json())["was_custom"] is True
        async with http.delete(f"{base}/api/settings/logo", headers=headers) as response:
            assert (await response.json())["was_custom"] is False
        async with http.get(f"{base}/logo") as response:
            assert await response.read() == packaged
    assert [(json.loads(row["payload"])["action"], json.loads(row["payload"])["by"]) for row in h.audit("settings.logo")] == [("set", "local"), ("reset", "local")]


# ================================================================== sign-in and roles
def test_passwords_are_stored_as_salted_scrypt_and_checked_in_constant_form():
    from hlbot.api import auth

    stored = auth.hash_password("correct horse battery")
    assert stored.startswith("scrypt$32768$8$1$") and "correct" not in stored
    assert stored != auth.hash_password("correct horse battery")  # a fresh salt every time
    assert auth.verify_password("correct horse battery", stored)
    for wrong in ("correct horse batterY", "", "x" * 300):
        assert not auth.verify_password(wrong, stored)
    for garbage in ("", "plain", "scrypt$1$2", "md5$1$8$1$AAAA$AAAA", "scrypt$x$8$1$AAAA$AAAA"):
        assert not auth.verify_password("anything", garbage)
    for bad in ("ab", "Has Space", "-lead", "a" * 33, "", None, "naïve"):
        with pytest.raises(auth.AuthError):
            auth.check_username(bad)
    assert auth.check_username("  Taufik.NR-26 ") == "taufik.nr-26"
    for bad in ("short", "x" * 257, None, 1234567890):
        with pytest.raises(auth.AuthError):
            auth.check_password(bad)
    with pytest.raises(auth.AuthError):
        auth.check_role("root")
    viewer, operator, admin = (auth.Principal("u", role, "session") for role in auth.ROLES)
    assert viewer.allows("viewer") and not viewer.allows("operator") and not viewer.allows("admin")
    assert operator.allows("viewer") and operator.allows("operator") and not operator.allows("admin")
    assert all(admin.allows(role) for role in auth.ROLES)


async def test_accounts_sessions_expiry_throttling_and_the_last_admin_rule(tmp_path):
    from hlbot.api import auth

    clock = ManualClock(START)
    db = fresh_db(tmp_path, clock)
    a = auth.Auth(db, clock)
    assert a.user_count() == 0 and a.session("anything") is None and a.session("") is None

    token, who = await a.setup_first_admin("Boss", "first-admin-password")
    assert (who.name, who.role, who.via) == ("boss", "admin", "session") and a.session(token) == who
    with pytest.raises(auth.AuthError) as refused:
        await a.setup_first_admin("second", "another-password-1")  # setup works exactly once
    assert refused.value.status == 409
    await a.create_user("vera", "viewer-password-1", "viewer", by="boss")
    with pytest.raises(auth.AuthError) as refused:
        await a.create_user("VERA", "viewer-password-2", "viewer", by="boss")
    assert refused.value.status == 409

    # the database holds neither a password nor a usable session token
    dump = json.dumps(db.all("SELECT * FROM users") + db.all("SELECT * FROM sessions") + db.all("SELECT payload FROM audit_events"))
    assert "first-admin-password" not in dump and "viewer-password-1" not in dump and token not in dump

    # the same answer for a wrong password, an unknown user and a disabled one
    for name, password in (("vera", "wrong-password-1"), ("nobody", "viewer-password-1")):
        with pytest.raises(auth.AuthError) as refused:
            await a.login(name, password, "10.0.0.9")
        assert refused.value.status == 401 and str(refused.value) == "wrong username or password"
    vera_token, vera = await a.login("Vera", "viewer-password-1", "10.0.0.9")
    assert vera.role == "viewer"

    # a role change applies to the open session at once; disabling ends it
    a.update_user("vera", "boss", role="operator")
    assert a.session(vera_token).role == "operator"
    a.update_user("vera", "boss", disabled=True)
    assert a.session(vera_token) is None
    with pytest.raises(auth.AuthError):
        await a.login("vera", "viewer-password-1", "10.0.0.9")
    a.update_user("vera", "boss", disabled=False)

    # the last active admin can be neither demoted, disabled nor removed
    for change in ({"role": "viewer"}, {"disabled": True}):
        with pytest.raises(auth.AuthError) as refused:
            a.update_user("boss", "boss", **change)
        assert refused.value.status == 409
    with pytest.raises(auth.AuthError):
        a.delete_user("boss", "boss")
    a.update_user("vera", "boss", role="admin")
    a.update_user("boss", "vera", role="viewer")  # fine once there is another admin
    assert a.session(token).role == "viewer"

    # a password change ends every other session of that account
    other, _ = await a.login("boss", "first-admin-password", "10.0.0.9")
    with pytest.raises(auth.AuthError) as refused:
        await a.change_own_password("boss", "not-the-password", "a-brand-new-password", token)
    assert refused.value.status == 403
    await a.change_own_password("boss", "first-admin-password", "a-brand-new-password", token)
    assert a.session(token) is not None and a.session(other) is None
    await a.login("boss", "a-brand-new-password", "10.0.0.9")

    # sessions end after twelve hours
    clock.advance(12 * 3600 - 5, monotonic=False)
    assert a.session(token) is not None
    clock.advance(10, monotonic=False)
    assert a.session(token) is None

    # five failures lock the name for the window, whatever comes next, then it opens again
    for _ in range(auth.THROTTLE_PER_USER):
        with pytest.raises(auth.AuthError) as refused:
            await a.login("boss", "guess-guess-guess", "10.0.0.7")
        assert refused.value.status == 401
    with pytest.raises(auth.AuthError) as refused:
        await a.login("boss", "a-brand-new-password", "10.0.0.7")  # even the right one
    assert refused.value.status == 429 and "try again" in str(refused.value)
    clock.advance(auth.THROTTLE_WINDOW_MS / 1000 + 1, monotonic=False)
    fresh, _ = await a.login("boss", "a-brand-new-password", "10.0.0.7")

    a.logout(fresh)
    assert a.session(fresh) is None
    a.logout("never-issued")  # harmless
    a.delete_user("boss", "vera")
    assert [u["username"] for u in a.list_users()] == ["vera"] and db.scalar("SELECT COUNT(*) FROM sessions WHERE user_id NOT IN (SELECT id FROM users)") == 0
    kinds = {row["kind"] for row in db.all("SELECT kind FROM audit_events")}
    assert {"user.created", "user.updated", "user.deleted", "user.password_changed", "auth.login", "auth.login_failed", "auth.logout"} <= kinds
    db.close()


async def test_login_setup_and_role_based_access_over_the_api(make):
    """First-run setup with the control token, sign-in by cookie, and
    what each role may and may not do."""
    port = free_port()
    h = await make(api_enabled=True, api_port=port)
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")
    base = f"http://127.0.0.1:{port}"
    token = h.settings.control_token_path.read_text()
    page = {"X-Requested-With": "hlbot"}  # what the dashboard's own requests carry

    def browser():
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))  # unsafe: accept cookies from an IP address

    async def call(http, method, path, expect, **kwargs):
        async with http.request(method, base + path, **kwargs) as response:
            assert response.status == expect, (method, path, response.status, await response.text())
            return await response.json() if response.content_type == "application/json" else await response.text()

    async with browser() as admin, browser() as oper, browser() as view, browser() as anon:
        # ---- a fresh installation: the sign-in page offers setup, which needs the control token
        assert await call(anon, "GET", "/api/public", 200) == {"name": "hlbot", "setup_required": True}
        boss = {"username": "boss", "password": "first-admin-password"}
        await call(anon, "POST", "/api/setup", 401, json={**boss, "token": "guess"}, headers=page)
        await call(anon, "POST", "/api/setup", 401, json=boss, headers=page)
        await call(anon, "POST", "/api/setup", 403, json={**boss, "token": token})  # not from the page
        assert h.app.api.auth.user_count() == 0
        async with admin.post(f"{base}/api/setup", json={**boss, "token": token}, headers=page) as response:
            assert response.status == 200 and await response.json() == {"ok": True, "user": "boss", "role": "admin"}
            cookie = response.headers["Set-Cookie"]
            assert cookie.startswith("hlbot_session=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
            assert "Max-Age=43200" in cookie
        assert (await call(anon, "GET", "/api/public", 200))["setup_required"] is False
        await call(anon, "POST", "/api/setup", 409, json={"username": "eve", "password": "another-password-1", "token": token}, headers=page)

        # ---- the signed-in admin gets the page; a signed-in browser is sent on from /login
        assert 'id="settings-dialog"' in await call(admin, "GET", "/", 200)
        async with admin.get(f"{base}/login", allow_redirects=False) as response:
            assert response.status == 302 and response.headers["Location"] == "/"
        assert await call(admin, "GET", "/api/session", 200) == {"user": "boss", "role": "admin", "via": "session", "name": "hlbot"}

        # ---- accounts
        await call(admin, "POST", "/api/users", 200, json={"username": "olga", "password": "operator-password", "role": "operator"}, headers=page)
        await call(admin, "POST", "/api/users", 200, json={"username": "vera", "password": "viewer-password-1", "role": "viewer"}, headers=page)
        await call(admin, "POST", "/api/users", 409, json={"username": "vera", "password": "viewer-password-2", "role": "viewer"}, headers=page)
        for bad in ({"username": "x", "password": "long-enough-password", "role": "viewer"}, {"username": "wendy", "password": "short", "role": "viewer"},
                    {"username": "wendy", "password": "long-enough-password", "role": "root"}, {"username": "wendy"}):
            await call(admin, "POST", "/api/users", 400, json=bad, headers=page)
        listed = await call(admin, "GET", "/api/users", 200)
        assert [(u["username"], u["role"], u["disabled"]) for u in listed["users"]] == [("boss", "admin", False), ("olga", "operator", False), ("vera", "viewer", False)]
        assert "password" not in json.dumps(listed) and "scrypt" not in json.dumps(listed)

        await call(anon, "POST", "/api/login", 401, json={"username": "olga", "password": "wrong-password-1"}, headers=page)
        await call(anon, "POST", "/api/login", 403, json={"username": "olga", "password": "operator-password"})  # not from the page
        await call(oper, "POST", "/api/login", 200, json={"username": "Olga", "password": "operator-password"}, headers=page)
        await call(view, "POST", "/api/login", 200, json={"username": "vera", "password": "viewer-password-1"}, headers=page)

        # ---- viewer: reads everything, changes nothing
        for path in ("/api/status", "/api/price", "/api/equity", "/api/trace?trade=1", "/metrics", "/api/session"):
            await call(view, "GET", path, 200)
        for method, path, body in (("POST", "/api/control/pause", {}), ("POST", "/api/control/kill", {"confirm": "KILL"}), ("POST", "/api/settings", {"name": "x"}),
                                   ("DELETE", "/api/settings/logo", None), ("GET", "/api/users", None), ("POST", "/api/users/vera", {"role": "admin"}),
                                   ("DELETE", "/api/users/boss", None)):
            answer = await call(view, method, path, 403, json=body, headers=page)
            assert "viewer" in answer["error"], path
        assert not h.health.operator_paused() and h.sim.positions[COIN][0] != 0

        # ---- operator: commands, but neither settings nor accounts
        assert (await call(oper, "POST", "/api/control/pause", 200, json={"reason": "desk", "by": "someone-else"}, headers=page))["state"] == "PAUSED"
        assert json.loads(h.audit("control.pause")[-1]["payload"])["by"] == "olga"  # the signed-in name, not what the browser sent
        await call(oper, "POST", "/api/control/resume", 200, json={}, headers=page)
        for method, path, body in (("POST", "/api/settings", {"name": "x"}), ("POST", "/api/settings/logo", None), ("GET", "/api/users", None),
                                   ("POST", "/api/users", {"username": "mallory", "password": "long-enough-password", "role": "admin"})):
            assert "operator" in (await call(oper, method, path, 403, json=body, headers=page))["error"]

        # ---- a cookie alone is not enough for a write: it must come from the dashboard itself
        await call(oper, "POST", "/api/control/pause", 403, json={})
        await call(oper, "POST", "/api/control/pause", 403, json={}, headers={**page, "Origin": "https://evil.example"})
        await call(oper, "POST", "/api/control/pause", 200, json={}, headers={**page, "Origin": base})
        await call(oper, "POST", "/api/control/resume", 200, json={}, headers=page)
        await call(oper, "GET", "/api/status", 401, headers={"X-Control-Token": "wrong"})  # a wrong token is not rescued by the cookie

        # ---- admin: the dashboard name
        assert (await call(admin, "POST", "/api/settings", 200, json={"name": "  Sniper   Execution "}, headers=page))["name"] == "Sniper Execution"
        assert (await call(anon, "GET", "/api/public", 200))["name"] == "Sniper Execution"
        assert (await call(view, "GET", "/api/session", 200))["name"] == "Sniper Execution"
        for bad in ({"name": "x" * 41}, {"name": "tab\there\u0007"}, {"name": 5}, {"title": "x"}, {}):
            await call(admin, "POST", "/api/settings", 400, json=bad, headers=page)
        markup = "<img src=x onerror=1>"
        assert (await call(admin, "POST", "/api/settings", 200, json={"name": markup}, headers=page))["name"] == markup  # kept as text; the page writes it as text
        assert (await call(admin, "POST", "/api/settings", 200, json={"name": ""}, headers=page))["name"] == "hlbot"

        # ---- admin: role, password and disabling take effect on the other browser at once
        await call(admin, "POST", "/api/users/vera", 200, json={"role": "operator"}, headers=page)
        await call(view, "POST", "/api/control/reconcile", 200, json={}, headers=page)
        await call(admin, "POST", "/api/users/vera", 200, json={"role": "viewer"}, headers=page)
        await call(view, "POST", "/api/control/reconcile", 403, json={}, headers=page)
        await call(admin, "POST", "/api/users/olga", 200, json={"password": "replaced-password-1"}, headers=page)
        await call(oper, "GET", "/api/status", 401)  # signed out everywhere
        await call(admin, "POST", "/api/users/vera", 200, json={"disabled": True}, headers=page)
        await call(view, "GET", "/api/status", 401)
        await call(admin, "POST", "/api/users/vera", 400, json={"shoe_size": 43}, headers=page)
        await call(admin, "POST", "/api/users/nobody", 404, json={"role": "viewer"}, headers=page)
        await call(admin, "POST", "/api/users/boss", 409, json={"role": "viewer"}, headers=page)  # the last admin
        await call(admin, "DELETE", "/api/users/boss", 409, headers=page)
        await call(admin, "DELETE", "/api/users/vera", 200, headers=page)

        # ---- own password, then sign out
        await call(admin, "POST", "/api/account/password", 403, json={"current": "nope-nope-nope", "new": "second-admin-password"}, headers=page)
        await call(admin, "POST", "/api/account/password", 400, json={"current": "first-admin-password", "new": "short"}, headers=page)
        await call(admin, "POST", "/api/account/password", 200, json={"current": "first-admin-password", "new": "second-admin-password"}, headers=page)
        await call(admin, "GET", "/api/status", 200)  # this browser stays signed in
        await call(admin, "POST", "/api/account/password", 400, json={"current": "x", "new": "y"}, headers={"X-Control-Token": token})  # the token has no password
        await call(admin, "POST", "/api/logout", 403)
        await call(admin, "POST", "/api/logout", 200, headers=page)
        await call(admin, "GET", "/api/status", 401)
        async with admin.get(f"{base}/", allow_redirects=False) as response:
            assert response.status == 302 and response.headers["Location"] == "/login"
        await call(admin, "POST", "/api/login", 401, json=boss, headers=page)  # the old password is gone
        await call(admin, "POST", "/api/login", 200, json={"username": "boss", "password": "second-admin-password"}, headers=page)

        # ---- the local token still does everything, for botctl
        local = {"X-Control-Token": token}
        assert [u["username"] for u in (await call(anon, "GET", "/api/users", 200, headers=local))["users"]] == ["boss", "olga"]
        await call(anon, "POST", "/api/control/pause", 200, json={"by": "cli"}, headers=local)
        assert json.loads(h.audit("control.pause")[-1]["payload"])["by"] == "cli"

    # nothing secret reached the audit trail or the log of settings
    trail = json.dumps([row["payload"] for row in h.db.all("SELECT payload FROM audit_events")])
    for secret in ("first-admin-password", "second-admin-password", "operator-password", "replaced-password-1", token):
        assert secret not in trail
    assert [json.loads(row["payload"])["name"] for row in h.audit("settings.name")] == ["Sniper Execution", "<img src=x onerror=1>", "hlbot"]


def test_sign_in_page_is_self_contained_and_keeps_no_credential():
    import re
    from importlib import resources

    page = resources.files("hlbot.api").joinpath("login.html").read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)="https?://', page) and "@import" not in page and "url(http" not in page
    assert 'id="login-form"' in page and 'id="setup-form" hidden' in page and 'autocomplete="current-password"' in page
    assert '"X-Requested-With": "hlbot"' in page and 'role="alert"' in page
    # the password goes to the server and nowhere else; the name from the server is written as text
    assert "localStorage.setItem(\"hlbot-theme\"" in page and "sessionStorage" not in page and "innerHTML" not in page
    assert page.count("localStorage.setItem") == 1


async def test_dashboard_published_through_a_proxy_or_tunnel_needs_its_address_declared(make, tmp_path):
    """A tunnel serves the page at https://bot.example.com and hands the
    request to the bot as plain HTTP for localhost. Signing in from that
    address is refused until it is declared, and the refusal says how."""
    page = {"X-Requested-With": "hlbot"}
    public = "https://bot.example.com"
    boss = {"username": "boss", "password": "first-admin-password"}

    async def run(h, port):
        base = f"http://127.0.0.1:{port}"
        await h.app.api.auth.create_user("boss", "first-admin-password", "admin", by="test")
        results = {}
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{base}/api/login", json=boss, headers={**page, "Origin": public}) as response:
                results["public"] = (response.status, (await response.json()).get("error", ""), response.headers.get("Set-Cookie", ""))
            async with http.post(f"{base}/api/login", json=boss, headers={**page, "Origin": "https://evil.example"}) as response:
                results["evil"] = (response.status, (await response.json()).get("error", ""))
            async with http.post(f"{base}/api/login", json=boss, headers={**page, "Origin": base}) as response:
                results["local"] = (response.status, response.headers.get("Set-Cookie", ""))
            async with http.get(f"{base}/health", headers={"Host": "bot.example.com"}) as response:
                results["public_host"] = response.status
            async with http.get(f"{base}/health", headers={"Host": "evil.example"}) as response:
                results["evil_host"] = response.status
            wrong = {"username": "boss", "password": "not-the-password"}
            async with http.post(f"{base}/api/login", json=wrong, headers={**page, "Origin": base, "CF-Connecting-IP": "203.0.113.7"}) as response:
                assert response.status == 401
            results["failed_from"] = json.loads(h.audit("auth.login_failed")[-1]["payload"])["address"]
        return results

    # ---- not declared: the default
    port = free_port()
    plain = await run(await make(api_enabled=True, api_port=port), port)
    status, error, cookie = plain["public"]
    assert status == 403 and cookie == "" and "API_PUBLIC_ORIGINS=https://bot.example.com" in error and "restart" in error
    assert plain["evil"][0] == 403 and plain["public_host"] == 403 and plain["evil_host"] == 403
    assert plain["local"][0] == 200 and "Secure" not in plain["local"][1]  # plain HTTP on the bot host itself
    assert plain["failed_from"] == "127.0.0.1"  # a forwarding header means nothing when no proxy is declared

    # ---- declared
    port = free_port()
    second = {"data_dir": str(tmp_path / "declared" / "data"), "log_dir": str(tmp_path / "declared" / "logs")}  # its own bot, its own lock
    declared = await run(await make(api_enabled=True, api_port=port, api_public_origins=(public,), **second), port)
    status, error, cookie = declared["public"]
    assert status == 200 and error == "" and cookie.startswith("hlbot_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie  # the browser is on HTTPS
    assert declared["evil"][0] == 403 and "evil.example" in declared["evil"][1]  # declaring one address opens no other
    assert declared["public_host"] == 200 and declared["evil_host"] == 403
    assert declared["local"][0] == 200 and "Secure" not in declared["local"][1]
    assert declared["failed_from"] == "203.0.113.7"  # throttling counts the visitor, not the proxy
