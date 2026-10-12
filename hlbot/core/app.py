<<<<<<< HEAD
"""Process wiring and lifecycle (PRD §9.6.8, §11).

Startup order, with entries disabled until recovery completes:

1. single-instance lock            (FR-061)
2. configuration and environment   (FR-082, FR-095)
3. database, migrations, config version
4. instruments, signer, limits     (FR-021, FR-070)
5. account state, fills, reconcile, protect-or-flatten every open trade
6. streams, Telegram listener with backfill
7. entries enabled
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal as signal_module
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from hlbot import __version__
from hlbot.config import Settings, load_agent_key, short_address
from hlbot.core.action_queue import ActionQueue
from hlbot.core.engine import Engine
from hlbot.core.health import BLOCK, Health
from hlbot.core.metrics import Metrics
from hlbot.core.notifier import Notifier, Sender
from hlbot.core.reconciler import Reconciler
from hlbot.core.signal_manager import SignalManager
from hlbot.db.database import Database, dumps
from hlbot.domain.parser import configure_channel_parsers
from hlbot.domain.planning import parse_iso
from hlbot.exchange.base import (
    ExchangeAdapter,
    FundingPayment,
    MarketContext,
    Quote,
    StreamHandlers,
)
from hlbot.redact import register_secrets
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.app")

HALT_ALERT_EVERY_S = 60.0
MARKET_FRESH_FOR_S = 10.0
STALE_POLL_S = 2.0


class AlreadyRunning(Exception):
    """A second instance against the same account refuses to start (FR-061)."""


class InstanceLock:
    """Advisory lock held for the life of the process. Unlike a process
    name scan (SEC-04), it cannot be bypassed by starting the program a
    different way, and the kernel releases it if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 - held open on purpose
        try:
            if sys.platform == "win32":  # pragma: no cover
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"another instance already holds {self.path}; two instances on one account would double-send orders"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def build_adapter(settings: Settings, db: Database, clock: Clock) -> ExchangeAdapter:
    """Paper mode never reaches ``load_agent_key`` (FR-080, SR-08)."""
    from hlbot.exchange.hyperliquid import HyperliquidAdapter

    common: dict[str, Any] = dict(
        rest_url=settings.rest_url,
        ws_url=settings.ws_url,
        is_mainnet=settings.market_data_env == "mainnet",
        coins=settings.coins,
        timeout_s=settings.rest_timeout_s,
        ws_stale_ms=settings.ws_stale_ms,
        clock=clock,
    )
    if settings.is_paper:
        from hlbot.exchange.paper import PaperAdapter
        from hlbot.exchange.sim import SimExchange

        market = HyperliquidAdapter(**common)  # read-only: no key, no account
        sim = SimExchange(equity=settings.paper_start_equity, clock=clock)
        return PaperAdapter(
            market, sim, load_state=lambda: db.kv_get("paper_state"), save_state=lambda state: db.kv_set("paper_state", state)
        )
    return HyperliquidAdapter(
        **common,
        account_address=settings.account_address,
        vault_address=settings.vault_address,
        agent_key=load_agent_key(settings),
    )


class BotApp:
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: Optional[ExchangeAdapter] = None,
        clock: Optional[Clock] = None,
        sender: Optional[Sender] = None,
        telegram_factory: Optional[Callable[..., Any]] = None,
        outlook_reader: Any = None,
        fast_poll_s: float = 0.4,
        watchdog_s: float = 1.0,
        market_fresh_for_s: float = MARKET_FRESH_FOR_S,
        install_signal_handlers: bool = True,
    ):
        self.s = settings
        self.clock = clock or Clock()
        self._adapter_override = adapter
        self._sender = sender
        self._telegram_factory = telegram_factory
        self._outlook_reader = outlook_reader
        self._fast_poll_s = fast_poll_s
        self._watchdog_s = watchdog_s
        self._market_fresh_for_s = market_fresh_for_s
        self._install_signals = install_signal_handlers
        self.stop_event = asyncio.Event()
        self.stop_reason = "stopped"
        self.lock = InstanceLock(settings.lock_path)
        self.metrics = Metrics()
        self.tasks: list[asyncio.Task] = []
        self.started = False
        self.telegram: Any = None
        self.api: Any = None
        self._ws_connected: Optional[bool] = None
        self._market_fresh_since: Optional[float] = None
        self._stream_seen = False
        self._last_halt_alert = 0.0
        self._last_stale_poll = 0.0
        self._telegram_error: Optional[str] = None
        # set in start()
        self.db: Database
        self.health: Health
        self.notifier: Notifier
        self.queue: ActionQueue
        self.adapter: ExchangeAdapter
        self.engine: Engine
        self.signals: SignalManager
        self.reconciler: Reconciler
        self.outlook: Any = None  # the scenario outlook; None when it is switched off
        self.control_token: str = ""

    # ================================================================== start
    async def start(self) -> None:
        s = self.s
        s.data_path.mkdir(parents=True, exist_ok=True)
        try:
            s.data_path.chmod(0o700)
        except OSError:
            pass
        self.lock.acquire()
        try:
            await self._start_locked()
        except BaseException:
            await self._teardown()
            raise

    async def _start_locked(self) -> None:
        s = self.s
        if s.stop_flag_path.exists():
            s.stop_flag_path.unlink()  # parity with the baseline: removed on next start
        register_secrets(s.secret_values())
        configure_channel_parsers(s.telegram_channel_parsers)

        self.db = Database(s.db_path, self.clock)
        self.db.migrate()
        self.db.prune_snapshots()
        config_version = self._config_version()
        logger.info("starting hlbot %s in %s mode", __version__, s.label, extra={"account": short_address(s.account_address)})
        if s.mode_note:
            logger.warning(s.mode_note)
        for warning in s.warnings:
            logger.warning(warning)

        self.health = Health(self.db, self.clock)
        self.notifier = Notifier(self.db, s, self.clock, self._sender)
        self.health.alert = self.notifier.alert
        self.queue = ActionQueue(self.db, self.health, self.clock, self.metrics)
        self.adapter = self._adapter_override or build_adapter(s, self.db, self.clock)
        self.engine = Engine(s, self.db, self.adapter, self.queue, self.health, self.notifier, self.clock, self.metrics, fast_poll_s=self._fast_poll_s)
        self.signals = SignalManager(s, self.db, self.engine, self.notifier, self.adapter, self.clock, self.metrics, config_version)
        self.reconciler = Reconciler(s, self.db, self.adapter, self.engine, self.health, self.notifier, self.queue, self.clock, self.metrics)
        self.db.audit("service.starting", {"version": __version__, "env": s.env, "requested_env": s.requested_env, "config_version": config_version})

        self.queue.start()
        self.health.refresh()
        await self.reconciler.startup()

        await self.adapter.start(
            StreamHandlers(
                on_quote=self._on_quote,
                on_context=self._on_context,
                on_fill=self.engine.on_fill,
                on_order_update=self.engine.on_order_update,
                on_liquidation=self.engine.on_liquidation,
                on_funding=self._on_funding,
                on_connection=self._on_connection,
            )
        )
        if s.telegram_enabled:
            await self._start_telegram()

        self.control_token = secrets.token_urlsafe(32)
        s.control_token_path.write_text(self.control_token)
        s.control_token_path.chmod(0o600)
        s.pid_path.write_text(str(os.getpid()))

        self.engine.entries_enabled = True
        self._spawn(self.notifier.run(self.stop_event), "notifier")
        self._spawn(self.reconciler.loop(self.stop_event), "reconciler")
        self._spawn(self._watchdog(), "watchdog")
        if self.telegram is not None:
            self._spawn(self.telegram.run(self.stop_event), "telegram")
        if s.outlook.enabled:
            from hlbot.core.outlook import OutlookService

            self.outlook = OutlookService(self, self._outlook_reader)
            self._spawn(self.outlook.run(self.stop_event), "outlook")
        if s.api_enabled:
            from hlbot.api.server import ApiServer

            self.api = ApiServer(self)
            await self.api.start()
        if self._install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal_module.SIGTERM, signal_module.SIGINT):
                try:
                    loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")
                except (NotImplementedError, RuntimeError):  # pragma: no cover - not on this platform
                    pass
        self.started = True
        self.notifier.notify(
            "service_status", state="started", mode=s.label,
            channel=", ".join(str(c) for c in s.telegram_channels) or "(telegram disabled)", note=s.mode_note,
        )
        self.db.audit("service.started", {"env": s.env, "health": self.health.state()})

    def _config_version(self) -> int:
        digest = self.s.config_hash()
        row = self.db.one("SELECT id FROM config_versions WHERE hash = ?", (digest,))
        if row:
            return int(row["id"])
        return self.db.insert("config_versions", hash=digest, effective_from=self.clock.iso(), settings=dumps(self.s.public_dict()))

    async def _start_telegram(self) -> None:
        from hlbot.config import ConfigError
        from hlbot.ingest.telegram import ChannelResolutionError, TelegramIngest

        factory = self._telegram_factory or TelegramIngest
        self.telegram = factory(
            self.s,
            on_message=self.ingest_message,
            on_edit=self.signals.on_edit,
            on_delete=self.signals.on_delete,
            alert=lambda title, detail="", critical=False: self.notifier.alert(title, detail, critical=critical),
        )
        try:
            await self.telegram.connect()
        except (ConfigError, ChannelResolutionError):
            raise  # exit non-zero: nothing can be listened to (FR-001)
        except Exception as exc:  # noqa: BLE001 - network trouble at startup is an outage, not a reason to exit (FR-006)
            self._telegram_error = type(exc).__name__
            logger.warning("Telegram unreachable at startup (%s); the listener will keep retrying", type(exc).__name__)
            self.notifier.alert("Telegram unreachable at startup", "The listener keeps retrying; signals are backfilled on connect.", critical=False)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self.tasks.append(task)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or self.stop_event.is_set():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("background task %s died: %r", task.get_name(), exc)
            self.metrics.inc("task_crashes")
            # Exit so the supervisor restarts into a clean recovery (NFR-005).
            self.request_stop(f"background task {task.get_name()} failed")

    # ================================================================== stream callbacks
    async def _on_quote(self, quote: Quote) -> None:
        await self.engine.on_quote(quote)
        await self.signals.on_quote(quote)

    async def _on_context(self, context: MarketContext) -> None:
        await self.engine.on_context(context)

    async def _on_funding(self, payment: FundingPayment) -> None:
        holding = self.engine.holding_trades(payment.coin)
        self.db.execute(
            "INSERT OR IGNORE INTO funding_payments (time, coin, usdc, rate, szi, trade_id) VALUES (?, ?, ?, ?, ?, ?)",
            (payment.time_ms, payment.coin, payment.usdc, payment.rate, payment.szi, holding[0]["id"] if holding else None),
        )

    async def _on_connection(self, connected: bool) -> None:
        previous = self._ws_connected
        self._ws_connected = connected
        self.db.audit("ws.connection", {"connected": connected})
        if connected and previous is False:
            self.metrics.inc("ws_reconnects")
            self.reconciler.kick()  # never trust the snapshot alone (§9.6.5)

    def ingest_message(self, message: dict[str, Any], source: str = "live") -> Optional[int]:
        signal_id = self.signals.ingest(message, source)
        if signal_id is not None:
            row = self.db.get("signals", signal_id)
            if row and row["state"] == "WAITING_PRICE":
                self.signals._evaluate(signal_id)
        return signal_id

    # ================================================================== watchdog
    async def _watchdog(self) -> None:
        """Once a second: stop flag, stream liveness, waiting signals,
        repeating HALTED alert, gauges (§10.7, §9.6.6)."""
        s = self.s
        heartbeat = s.data_path / "heartbeat"
        while not self.stop_event.is_set():
            try:
                if s.stop_flag_path.exists():
                    self.request_stop("stop flag")
                    break
                now_ms = self.clock.now_ms()
                mono = self.clock.monotonic()
                stream = self.adapter.stream_health()
                age = now_ms - stream.last_message_ms if stream.last_message_ms else None
                stale = not stream.connected or age is None or age > s.ws_stale_ms
                if stale:
                    self._market_fresh_since = None
                    self.health.set_condition(
                        "market_data_stale", "market data stream is disconnected or silent", level=BLOCK, auto_resume=True, alert=False
                    )
                    if self.engine.open_trades() and mono - self._last_stale_poll >= STALE_POLL_S:
                        self._last_stale_poll = mono
                        self.reconciler.kick()  # manage open trades by REST polling while the stream is down
                    since = self.db.one("SELECT set_at FROM conditions WHERE key = 'market_data_stale'")
                    if since:
                        if (self.clock.now() - parse_iso(since["set_at"])).total_seconds() >= 30:
                            self.engine._alert_once("ws_down", "Market data stream down for 30 s", "Entries are blocked; open trades are managed over REST.", critical=False)
                else:
                    if self._market_fresh_since is None:
                        self._market_fresh_since = mono
                    # §10.7: resumes automatically once the stream has been fresh for 10 s.
                    # The very first connection after start is not a recovery: no waiting period.
                    settled = not self._stream_seen or mono - self._market_fresh_since >= self._market_fresh_for_s
                    self._stream_seen = True
                    if settled and self.health.clear_condition("market_data_stale"):
                        self.engine._alerted.discard("ws_down")
                await self.signals.tick()

                if self.health.state() == "HALTED" and self.health.halt_unacknowledged() and mono - self._last_halt_alert >= HALT_ALERT_EVERY_S:
                    self._last_halt_alert = mono
                    reasons = "; ".join(c["reason"] for c in self.health.conditions() if c["level"] == "halt")
                    self.notifier.alert("STILL HALTED — manual action required", f"{reasons}\nAcknowledge: botctl ack   Resume: botctl resume", critical=True)

                self.metrics.gauge("ws_connected", 1 if stream.connected else 0)
                self.metrics.gauge("ws_message_age_ms", age if age is not None else -1)
                self.metrics.gauge("ws_reconnects_total", stream.reconnects)
                self.metrics.gauge("rest_latency_ms", stream.rest_ms if stream.rest_ms is not None else -1)
                self.metrics.gauge("ws_ping_ms", stream.ping_ms if stream.ping_ms is not None else -1)
                for coin in s.coins:
                    quote_age = self.engine.quote_age_ms(coin)
                    self.metrics.gauge(f"quote_age_ms_{coin.replace(':', '_')}", quote_age if quote_age is not None else -1)
                self.metrics.gauge("outbox_depth", self.notifier.pending())
                self.metrics.gauge("open_trades", len(self.engine.open_trades()))
                self.metrics.gauge("waiting_signals", len(self.signals.waiting()))
                self.metrics.gauge("health_state", {"HEALTHY": 0, "DEGRADED": 1, "PAUSED": 2, "HALTED": 3}[self.health.refresh()])
                heartbeat.write_text(self.clock.iso())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watchdog must not die
                logger.exception("watchdog pass failed")
            await wait_event(self.stop_event, self._watchdog_s)

    # ================================================================== stop
    def request_stop(self, reason: str = "stopped") -> None:
        if not self.stop_event.is_set():
            self.stop_reason = reason
            logger.info("stop requested: %s", reason)
            self.stop_event.set()

    async def run(self) -> int:
        await self.start()
        await self.stop_event.wait()
        await self.shutdown()
        return 0

    async def shutdown(self) -> None:
        """Graceful stop. Exchange-resident SL and TP orders stay in place
        and keep protecting open positions."""
        if not self.started:
            await self._teardown()
            return
        self.started = False
        self.stop_event.set()
        self.engine.entries_enabled = False
        open_trades = len(self.engine.open_trades())
        self.notifier.notify(
            "service_status", state="stopped", mode=self.s.label,
            channel=", ".join(str(c) for c in self.s.telegram_channels) or "(telegram disabled)",
            reason=self.stop_reason + (f"; {open_trades} trade(s) remain protected by exchange-resident orders" if open_trades else ""),
        )
        self.db.audit("service.stopped", {"reason": self.stop_reason, "open_trades": open_trades})
        await self.signals.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        try:
            await self.notifier.flush(timeout_s=8.0)
        except Exception:  # noqa: BLE001
            logger.exception("final notification flush failed")
        await self._teardown()

    async def _teardown(self) -> None:
        for step in (
            lambda: self.api.stop() if self.api else None,
            lambda: self.outlook.close() if self.outlook else None,
            lambda: self.telegram.disconnect() if self.telegram else None,
            lambda: self.engine.stop() if hasattr(self, "engine") else None,
            lambda: self.queue.stop() if hasattr(self, "queue") else None,
            lambda: self.adapter.stop() if hasattr(self, "adapter") else None,
        ):
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("teardown step failed")
        if hasattr(self, "db"):
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                pass
        for path in (self.s.pid_path, self.s.control_token_path, self.s.stop_flag_path):
            try:
                path.unlink()
            except OSError:
                pass
        self.lock.release()

    # ================================================================== operator commands (validated here, never in a client)
    async def command(self, name: str, args: Optional[dict[str, Any]] = None, by: str = "operator") -> dict[str, Any]:
        args = args or {}
        if name == "pause":
            self.health.pause(str(args.get("reason") or "operator"), by)
            return {"ok": True, "state": self.health.state()}
        if name == "resume":
            cleared = self.health.resume(by)
            self.reconciler.kick()
            return {"ok": True, "cleared": cleared, "state": self.health.state()}
        if name == "ack":
            self.health.acknowledge(by)
            return {"ok": True}
        if name == "kill":
            report = await self.engine.kill(by)
            self.reconciler.kick()  # refresh the displayed exchange view straight away
            return {"ok": True, **report}
        if name == "close":
            trade_id = int(args["trade_id"])
            accepted = self.engine.request_close(trade_id, "manual")
            self.reconciler.kick()
            return {"ok": accepted, "trade_id": trade_id}
        if name == "reconcile":
            target = args.get("trade_id")
            if args.get("adopt") and target is not None:
                return {"ok": await self.engine.adopt(int(target))}
            if args.get("close") and target is not None:
                trade = self.db.get("trades", int(target))
                if trade and trade["state"] == "ENTERING":
                    return {"ok": self.engine.abort_entry(int(target))}
                return {"ok": self.engine.request_close(int(target), "manual")}
            return await self.reconciler.run_once("command")
        if name == "modify_sl":
            return {"ok": await self.engine.modify_sl(int(args["trade_id"]), trigger=float(args["price"]), reason="operator")}
        if name == "inject":
            # Rehearsal aid (UC-08): feed a message as if a channel posted it. Never on mainnet.
            if self.s.env == "mainnet":
                return {"ok": False, "error": "inject is disabled on mainnet"}
            last = int(self.db.scalar("SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE channel_id = 0", default=0))
            signal_id = self.ingest_message(
                {
                    "channel_id": 0, "message_id": last + 1, "channel_ref": str(args.get("channel") or "inject"),
                    "date": self.clock.iso(), "text": str(args.get("text") or ""), "has_media": False, "reply_to": None,
                },
                "inject",
            )
            row = self.db.get("signals", signal_id) if signal_id else None
            return {"ok": row is not None, "signal_id": signal_id, "state": row["state"] if row else None, "reason": row["reason"] if row else None}
        return {"ok": False, "error": f"unknown command {name!r}"}
=======
"""Process wiring and lifecycle (PRD §9.6.8, §11).

Startup order, with entries disabled until recovery completes:

1. single-instance lock            (FR-061)
2. configuration and environment   (FR-082, FR-095)
3. database, migrations, config version
4. instruments, signer, limits     (FR-021, FR-070)
5. account state, fills, reconcile, protect-or-flatten every open trade
6. streams, Telegram listener with backfill
7. entries enabled
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal as signal_module
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from hlbot import __version__
from hlbot.config import Settings, load_agent_key, short_address
from hlbot.core.action_queue import ActionQueue
from hlbot.core.engine import Engine
from hlbot.core.health import BLOCK, Health
from hlbot.core.metrics import Metrics
from hlbot.core.notifier import Notifier, Sender
from hlbot.core.reconciler import Reconciler
from hlbot.core.signal_manager import SignalManager
from hlbot.db.database import Database, dumps
from hlbot.domain.parser import configure_channel_parsers
from hlbot.domain.planning import parse_iso
from hlbot.exchange.base import (
    ExchangeAdapter,
    FundingPayment,
    MarketContext,
    Quote,
    StreamHandlers,
)
from hlbot.redact import register_secrets
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.app")

HALT_ALERT_EVERY_S = 60.0
MARKET_FRESH_FOR_S = 10.0
STALE_POLL_S = 2.0


class AlreadyRunning(Exception):
    """A second instance against the same account refuses to start (FR-061)."""


class InstanceLock:
    """Advisory lock held for the life of the process. Unlike a process
    name scan (SEC-04), it cannot be bypassed by starting the program a
    different way, and the kernel releases it if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")  # noqa: SIM115 - held open on purpose
        try:
            if sys.platform == "win32":  # pragma: no cover
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                f"another instance already holds {self.path}; two instances on one account would double-send orders"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


def build_adapter(settings: Settings, db: Database, clock: Clock) -> ExchangeAdapter:
    """Paper mode never reaches ``load_agent_key`` (FR-080, SR-08)."""
    from hlbot.exchange.hyperliquid import HyperliquidAdapter

    common: dict[str, Any] = dict(
        rest_url=settings.rest_url,
        ws_url=settings.ws_url,
        is_mainnet=settings.market_data_env == "mainnet",
        coins=settings.coins,
        timeout_s=settings.rest_timeout_s,
        ws_stale_ms=settings.ws_stale_ms,
        clock=clock,
    )
    if settings.is_paper:
        from hlbot.exchange.paper import PaperAdapter
        from hlbot.exchange.sim import SimExchange

        market = HyperliquidAdapter(**common)  # read-only: no key, no account
        sim = SimExchange(equity=settings.paper_start_equity, clock=clock)
        return PaperAdapter(
            market, sim, load_state=lambda: db.kv_get("paper_state"), save_state=lambda state: db.kv_set("paper_state", state)
        )
    return HyperliquidAdapter(
        **common,
        account_address=settings.account_address,
        vault_address=settings.vault_address,
        agent_key=load_agent_key(settings),
    )


class BotApp:
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: Optional[ExchangeAdapter] = None,
        clock: Optional[Clock] = None,
        sender: Optional[Sender] = None,
        telegram_factory: Optional[Callable[..., Any]] = None,
        fast_poll_s: float = 0.4,
        watchdog_s: float = 1.0,
        market_fresh_for_s: float = MARKET_FRESH_FOR_S,
        install_signal_handlers: bool = True,
    ):
        self.s = settings
        self.clock = clock or Clock()
        self._adapter_override = adapter
        self._sender = sender
        self._telegram_factory = telegram_factory
        self._fast_poll_s = fast_poll_s
        self._watchdog_s = watchdog_s
        self._market_fresh_for_s = market_fresh_for_s
        self._install_signals = install_signal_handlers
        self.stop_event = asyncio.Event()
        self.stop_reason = "stopped"
        self.lock = InstanceLock(settings.lock_path)
        self.metrics = Metrics()
        self.tasks: list[asyncio.Task] = []
        self.started = False
        self.telegram: Any = None
        self.api: Any = None
        self._ws_connected: Optional[bool] = None
        self._market_fresh_since: Optional[float] = None
        self._stream_seen = False
        self._last_halt_alert = 0.0
        self._last_stale_poll = 0.0
        self._telegram_error: Optional[str] = None
        # set in start()
        self.db: Database
        self.health: Health
        self.notifier: Notifier
        self.queue: ActionQueue
        self.adapter: ExchangeAdapter
        self.engine: Engine
        self.signals: SignalManager
        self.reconciler: Reconciler
        self.control_token: str = ""

    # ================================================================== start
    async def start(self) -> None:
        s = self.s
        s.data_path.mkdir(parents=True, exist_ok=True)
        try:
            s.data_path.chmod(0o700)
        except OSError:
            pass
        self.lock.acquire()
        try:
            await self._start_locked()
        except BaseException:
            await self._teardown()
            raise

    async def _start_locked(self) -> None:
        s = self.s
        if s.stop_flag_path.exists():
            s.stop_flag_path.unlink()  # parity with the baseline: removed on next start
        register_secrets(s.secret_values())
        configure_channel_parsers(s.telegram_channel_parsers)

        self.db = Database(s.db_path, self.clock)
        self.db.migrate()
        self.db.prune_snapshots()
        config_version = self._config_version()
        logger.info("starting hlbot %s in %s mode", __version__, s.label, extra={"account": short_address(s.account_address)})
        if s.mode_note:
            logger.warning(s.mode_note)
        for warning in s.warnings:
            logger.warning(warning)

        self.health = Health(self.db, self.clock)
        self.notifier = Notifier(self.db, s, self.clock, self._sender)
        self.health.alert = self.notifier.alert
        self.queue = ActionQueue(self.db, self.health, self.clock, self.metrics)
        self.adapter = self._adapter_override or build_adapter(s, self.db, self.clock)
        self.engine = Engine(s, self.db, self.adapter, self.queue, self.health, self.notifier, self.clock, self.metrics, fast_poll_s=self._fast_poll_s)
        self.signals = SignalManager(s, self.db, self.engine, self.notifier, self.adapter, self.clock, self.metrics, config_version)
        self.reconciler = Reconciler(s, self.db, self.adapter, self.engine, self.health, self.notifier, self.queue, self.clock, self.metrics)
        self.db.audit("service.starting", {"version": __version__, "env": s.env, "requested_env": s.requested_env, "config_version": config_version})

        self.queue.start()
        self.health.refresh()
        await self.reconciler.startup()

        await self.adapter.start(
            StreamHandlers(
                on_quote=self._on_quote,
                on_context=self._on_context,
                on_fill=self.engine.on_fill,
                on_order_update=self.engine.on_order_update,
                on_liquidation=self.engine.on_liquidation,
                on_funding=self._on_funding,
                on_connection=self._on_connection,
            )
        )
        if s.telegram_enabled:
            await self._start_telegram()

        self.control_token = secrets.token_urlsafe(32)
        s.control_token_path.write_text(self.control_token)
        s.control_token_path.chmod(0o600)
        s.pid_path.write_text(str(os.getpid()))

        self.engine.entries_enabled = True
        self._spawn(self.notifier.run(self.stop_event), "notifier")
        self._spawn(self.reconciler.loop(self.stop_event), "reconciler")
        self._spawn(self._watchdog(), "watchdog")
        if self.telegram is not None:
            self._spawn(self.telegram.run(self.stop_event), "telegram")
        if s.api_enabled:
            from hlbot.api.server import ApiServer

            self.api = ApiServer(self)
            await self.api.start()
        if self._install_signals:
            loop = asyncio.get_running_loop()
            for sig in (signal_module.SIGTERM, signal_module.SIGINT):
                try:
                    loop.add_signal_handler(sig, self.request_stop, f"signal {sig.name}")
                except (NotImplementedError, RuntimeError):  # pragma: no cover - not on this platform
                    pass
        self.started = True
        self.notifier.notify(
            "service_status", state="started", mode=s.label,
            channel=", ".join(str(c) for c in s.telegram_channels) or "(telegram disabled)", note=s.mode_note,
        )
        self.db.audit("service.started", {"env": s.env, "health": self.health.state()})

    def _config_version(self) -> int:
        digest = self.s.config_hash()
        row = self.db.one("SELECT id FROM config_versions WHERE hash = ?", (digest,))
        if row:
            return int(row["id"])
        return self.db.insert("config_versions", hash=digest, effective_from=self.clock.iso(), settings=dumps(self.s.public_dict()))

    async def _start_telegram(self) -> None:
        from hlbot.config import ConfigError
        from hlbot.ingest.telegram import ChannelResolutionError, TelegramIngest

        factory = self._telegram_factory or TelegramIngest
        self.telegram = factory(
            self.s,
            on_message=self.ingest_message,
            on_edit=self.signals.on_edit,
            on_delete=self.signals.on_delete,
            alert=lambda title, detail="", critical=False: self.notifier.alert(title, detail, critical=critical),
        )
        try:
            await self.telegram.connect()
        except (ConfigError, ChannelResolutionError):
            raise  # exit non-zero: nothing can be listened to (FR-001)
        except Exception as exc:  # noqa: BLE001 - network trouble at startup is an outage, not a reason to exit (FR-006)
            self._telegram_error = type(exc).__name__
            logger.warning("Telegram unreachable at startup (%s); the listener will keep retrying", type(exc).__name__)
            self.notifier.alert("Telegram unreachable at startup", "The listener keeps retrying; signals are backfilled on connect.", critical=False)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self.tasks.append(task)

    def _task_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or self.stop_event.is_set():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("background task %s died: %r", task.get_name(), exc)
            self.metrics.inc("task_crashes")
            # Exit so the supervisor restarts into a clean recovery (NFR-005).
            self.request_stop(f"background task {task.get_name()} failed")

    # ================================================================== stream callbacks
    async def _on_quote(self, quote: Quote) -> None:
        await self.engine.on_quote(quote)
        await self.signals.on_quote(quote)

    async def _on_context(self, context: MarketContext) -> None:
        await self.engine.on_context(context)

    async def _on_funding(self, payment: FundingPayment) -> None:
        holding = self.engine.holding_trades(payment.coin)
        self.db.execute(
            "INSERT OR IGNORE INTO funding_payments (time, coin, usdc, rate, szi, trade_id) VALUES (?, ?, ?, ?, ?, ?)",
            (payment.time_ms, payment.coin, payment.usdc, payment.rate, payment.szi, holding[0]["id"] if holding else None),
        )

    async def _on_connection(self, connected: bool) -> None:
        previous = self._ws_connected
        self._ws_connected = connected
        self.db.audit("ws.connection", {"connected": connected})
        if connected and previous is False:
            self.metrics.inc("ws_reconnects")
            self.reconciler.kick()  # never trust the snapshot alone (§9.6.5)

    def ingest_message(self, message: dict[str, Any], source: str = "live") -> Optional[int]:
        signal_id = self.signals.ingest(message, source)
        if signal_id is not None:
            row = self.db.get("signals", signal_id)
            if row and row["state"] == "WAITING_PRICE":
                self.signals._evaluate(signal_id)
        return signal_id

    # ================================================================== watchdog
    async def _watchdog(self) -> None:
        """Once a second: stop flag, stream liveness, waiting signals,
        repeating HALTED alert, gauges (§10.7, §9.6.6)."""
        s = self.s
        heartbeat = s.data_path / "heartbeat"
        while not self.stop_event.is_set():
            try:
                if s.stop_flag_path.exists():
                    self.request_stop("stop flag")
                    break
                now_ms = self.clock.now_ms()
                mono = self.clock.monotonic()
                stream = self.adapter.stream_health()
                age = now_ms - stream.last_message_ms if stream.last_message_ms else None
                stale = not stream.connected or age is None or age > s.ws_stale_ms
                if stale:
                    self._market_fresh_since = None
                    self.health.set_condition(
                        "market_data_stale", "market data stream is disconnected or silent", level=BLOCK, auto_resume=True, alert=False
                    )
                    if self.engine.open_trades() and mono - self._last_stale_poll >= STALE_POLL_S:
                        self._last_stale_poll = mono
                        self.reconciler.kick()  # manage open trades by REST polling while the stream is down
                    since = self.db.one("SELECT set_at FROM conditions WHERE key = 'market_data_stale'")
                    if since:
                        if (self.clock.now() - parse_iso(since["set_at"])).total_seconds() >= 30:
                            self.engine._alert_once("ws_down", "Market data stream down for 30 s", "Entries are blocked; open trades are managed over REST.", critical=False)
                else:
                    if self._market_fresh_since is None:
                        self._market_fresh_since = mono
                    # §10.7: resumes automatically once the stream has been fresh for 10 s.
                    # The very first connection after start is not a recovery: no waiting period.
                    settled = not self._stream_seen or mono - self._market_fresh_since >= self._market_fresh_for_s
                    self._stream_seen = True
                    if settled and self.health.clear_condition("market_data_stale"):
                        self.engine._alerted.discard("ws_down")
                await self.signals.tick()

                if self.health.state() == "HALTED" and self.health.halt_unacknowledged() and mono - self._last_halt_alert >= HALT_ALERT_EVERY_S:
                    self._last_halt_alert = mono
                    reasons = "; ".join(c["reason"] for c in self.health.conditions() if c["level"] == "halt")
                    self.notifier.alert("STILL HALTED — manual action required", f"{reasons}\nAcknowledge: botctl ack   Resume: botctl resume", critical=True)

                self.metrics.gauge("ws_connected", 1 if stream.connected else 0)
                self.metrics.gauge("ws_message_age_ms", age if age is not None else -1)
                self.metrics.gauge("ws_reconnects_total", stream.reconnects)
                self.metrics.gauge("rest_latency_ms", stream.rest_ms if stream.rest_ms is not None else -1)
                self.metrics.gauge("ws_ping_ms", stream.ping_ms if stream.ping_ms is not None else -1)
                for coin in s.coins:
                    quote_age = self.engine.quote_age_ms(coin)
                    self.metrics.gauge(f"quote_age_ms_{coin.replace(':', '_')}", quote_age if quote_age is not None else -1)
                self.metrics.gauge("outbox_depth", self.notifier.pending())
                self.metrics.gauge("open_trades", len(self.engine.open_trades()))
                self.metrics.gauge("waiting_signals", len(self.signals.waiting()))
                self.metrics.gauge("health_state", {"HEALTHY": 0, "DEGRADED": 1, "PAUSED": 2, "HALTED": 3}[self.health.refresh()])
                heartbeat.write_text(self.clock.iso())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watchdog must not die
                logger.exception("watchdog pass failed")
            await wait_event(self.stop_event, self._watchdog_s)

    # ================================================================== stop
    def request_stop(self, reason: str = "stopped") -> None:
        if not self.stop_event.is_set():
            self.stop_reason = reason
            logger.info("stop requested: %s", reason)
            self.stop_event.set()

    async def run(self) -> int:
        await self.start()
        await self.stop_event.wait()
        await self.shutdown()
        return 0

    async def shutdown(self) -> None:
        """Graceful stop. Exchange-resident SL and TP orders stay in place
        and keep protecting open positions."""
        if not self.started:
            await self._teardown()
            return
        self.started = False
        self.stop_event.set()
        self.engine.entries_enabled = False
        open_trades = len(self.engine.open_trades())
        self.notifier.notify(
            "service_status", state="stopped", mode=self.s.label,
            channel=", ".join(str(c) for c in self.s.telegram_channels) or "(telegram disabled)",
            reason=self.stop_reason + (f"; {open_trades} trade(s) remain protected by exchange-resident orders" if open_trades else ""),
        )
        self.db.audit("service.stopped", {"reason": self.stop_reason, "open_trades": open_trades})
        await self.signals.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        try:
            await self.notifier.flush(timeout_s=8.0)
        except Exception:  # noqa: BLE001
            logger.exception("final notification flush failed")
        await self._teardown()

    async def _teardown(self) -> None:
        for step in (
            lambda: self.api.stop() if self.api else None,
            lambda: self.telegram.disconnect() if self.telegram else None,
            lambda: self.engine.stop() if hasattr(self, "engine") else None,
            lambda: self.queue.stop() if hasattr(self, "queue") else None,
            lambda: self.adapter.stop() if hasattr(self, "adapter") else None,
        ):
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("teardown step failed")
        if hasattr(self, "db"):
            try:
                self.db.close()
            except Exception:  # noqa: BLE001
                pass
        for path in (self.s.pid_path, self.s.control_token_path, self.s.stop_flag_path):
            try:
                path.unlink()
            except OSError:
                pass
        self.lock.release()

    # ================================================================== operator commands (validated here, never in a client)
    async def command(self, name: str, args: Optional[dict[str, Any]] = None, by: str = "operator") -> dict[str, Any]:
        args = args or {}
        if name == "pause":
            self.health.pause(str(args.get("reason") or "operator"), by)
            return {"ok": True, "state": self.health.state()}
        if name == "resume":
            cleared = self.health.resume(by)
            self.reconciler.kick()
            return {"ok": True, "cleared": cleared, "state": self.health.state()}
        if name == "ack":
            self.health.acknowledge(by)
            return {"ok": True}
        if name == "kill":
            report = await self.engine.kill(by)
            self.reconciler.kick()  # refresh the displayed exchange view straight away
            return {"ok": True, **report}
        if name == "close":
            trade_id = int(args["trade_id"])
            accepted = self.engine.request_close(trade_id, "manual")
            self.reconciler.kick()
            return {"ok": accepted, "trade_id": trade_id}
        if name == "reconcile":
            target = args.get("trade_id")
            if args.get("adopt") and target is not None:
                return {"ok": await self.engine.adopt(int(target))}
            if args.get("close") and target is not None:
                trade = self.db.get("trades", int(target))
                if trade and trade["state"] == "ENTERING":
                    return {"ok": self.engine.abort_entry(int(target))}
                return {"ok": self.engine.request_close(int(target), "manual")}
            return await self.reconciler.run_once("command")
        if name == "modify_sl":
            return {"ok": await self.engine.modify_sl(int(args["trade_id"]), trigger=float(args["price"]), reason="operator")}
        if name == "inject":
            # Rehearsal aid (UC-08): feed a message as if a channel posted it. Never on mainnet.
            if self.s.env == "mainnet":
                return {"ok": False, "error": "inject is disabled on mainnet"}
            last = int(self.db.scalar("SELECT COALESCE(MAX(message_id), 0) FROM messages WHERE channel_id = 0", default=0))
            signal_id = self.ingest_message(
                {
                    "channel_id": 0, "message_id": last + 1, "channel_ref": str(args.get("channel") or "inject"),
                    "date": self.clock.iso(), "text": str(args.get("text") or ""), "has_media": False, "reply_to": None,
                },
                "inject",
            )
            row = self.db.get("signals", signal_id) if signal_id else None
            return {"ok": row is not None, "signal_id": signal_id, "state": row["state"] if row else None, "reason": row["reason"] if row else None}
        return {"ok": False, "error": f"unknown command {name!r}"}
>>>>>>> a98f46a (fixing parser buy)
