"""Telegram ingestion with Telethon (FR-001 .. FR-006).

Ported from the baseline ``app/message_scraper.py`` and hardened:

* every message carries ``(channel_id, message_id)``;
* the last N messages per channel are backfilled on startup and after
  every reconnect (fixes LIM-06);
* an outage of any length never ends the process: the listener
  reconnects with capped exponential backoff, alerting once at the
  start of the outage and once at recovery (fixes LIM-05);
* edits and deletions are observed and reported (fixes LIM-18).

The Telethon session file is a full-account credential: it must live
outside the repository with owner-only permissions (SR-04).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Callable
from typing import Any, Optional, Union

from hlbot.config import ConfigError, Settings, check_owner_only
from hlbot.timeutil import wait_event

logger = logging.getLogger("hlbot.telegram")

BACKOFF_START_S = 2.0
BACKOFF_CAP_S = 60.0


class ChannelResolutionError(Exception):
    """None of the configured channels could be resolved."""


def format_message(message: Any, channel_ref: Union[str, int]) -> dict[str, Any]:
    """Telethon message -> plain dict. Only Telegram-provided metadata is
    captured; ``text`` is the raw, unmodified message."""
    reply_to = None
    if getattr(message, "reply_to", None) is not None:
        reply_to = getattr(message.reply_to, "reply_to_msg_id", None)
    return {
        "message_id": message.id,
        "channel_id": message.chat_id,
        "channel_ref": str(channel_ref),
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
        "text": message.message if getattr(message, "message", None) else "",
        "has_media": getattr(message, "media", None) is not None,
        "reply_to": reply_to,
    }


def build_client(settings: Settings) -> Any:
    """Construct (not connect) a Telethon client. Imported lazily so the
    rest of the bot runs without Telethon when Telegram is disabled."""
    from telethon import TelegramClient

    path = settings.session_path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if path.exists():
        check_owner_only(path, "Telegram session file")
    if settings.telegram_api_id is None or not settings.telegram_api_hash:
        raise ConfigError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
    # Telethon appends ".session" to a name without that suffix.
    name = str(path)[: -len(".session")] if str(path).endswith(".session") else str(path)
    # Telethon creates a missing session file here, under the process
    # umask. Without this the next start refuses the file it just wrote.
    old_mask = os.umask(0o077)
    try:
        return TelegramClient(
            name, settings.telegram_api_id, settings.telegram_api_hash,
            connection_retries=3, retry_delay=2, auto_reconnect=True, request_retries=3,
        )
    finally:
        os.umask(old_mask)


class TelegramIngest:
    def __init__(
        self,
        settings: Settings,
        *,
        on_message: Callable[[dict[str, Any], str], Any],
        on_edit: Callable[[int, int, str, Optional[str]], Any],
        on_delete: Callable[[Optional[int], list[int]], Any],
        alert: Callable[..., Any],
        client_factory: Optional[Callable[[Settings], Any]] = None,
        backoff_scale: float = 1.0,
    ):
        self.s = settings
        self.on_message = on_message
        self.on_edit = on_edit
        self.on_delete = on_delete
        self.alert = alert
        self._factory = client_factory or build_client
        self._scale = backoff_scale
        self._client: Any = None
        self._entities: dict[Any, Any] = {}  # channel ref -> entity
        self._ref_by_id: dict[int, Union[str, int]] = {}
        self._registered = False
        self.connected = False
        self.outage_alerted = False
        self.resolved: list[Union[str, int]] = []

    # ------------------------------------------------------------ channels
    async def _resolve(self) -> None:
        """A channel that cannot be resolved is skipped with a warning;
        only zero resolved channels is an error (FR-001)."""
        failures: list[str] = []
        self._entities.clear()
        self._ref_by_id.clear()
        for channel in self.s.telegram_channels:
            try:
                entity = await self._client.get_entity(channel)
            except Exception as exc:  # noqa: BLE001 - Telethon raises many types
                logger.warning("skipping channel %s: %s", channel, type(exc).__name__)
                failures.append(f"{channel}: {type(exc).__name__}")
                continue
            self._entities[channel] = entity
            entity_id = getattr(entity, "id", None)
            if entity_id is not None:
                self._ref_by_id[int(entity_id)] = channel
        self.resolved = list(self._entities)
        if not self._entities:
            raise ChannelResolutionError("none of the configured channels could be resolved: " + "; ".join(failures))

    def _ref_for(self, chat_id: Optional[int]) -> Union[str, int, None]:
        if chat_id is None:
            return None
        if chat_id in self._ref_by_id:
            return self._ref_by_id[chat_id]
        # Telethon reports channels as -100<id>; entity.id is the bare id.
        text = str(chat_id)
        if text.startswith("-100") and int(text[4:]) in self._ref_by_id:
            return self._ref_by_id[int(text[4:])]
        return chat_id

    def _register(self) -> None:
        from telethon import events

        chats = list(self._entities.values())

        @self._client.on(events.NewMessage(chats=chats))
        async def _new(event: Any) -> None:
            await self._deliver(format_message(event.message, self._ref_for(event.chat_id) or event.chat_id), "live")

        @self._client.on(events.MessageEdited(chats=chats))
        async def _edited(event: Any) -> None:
            message = event.message
            edited = message.edit_date.isoformat() if getattr(message, "edit_date", None) else None
            await _call(self.on_edit, message.chat_id, message.id, message.message or "", edited)

        @self._client.on(events.MessageDeleted(chats=chats))
        async def _deleted(event: Any) -> None:
            await _call(self.on_delete, getattr(event, "chat_id", None), list(event.deleted_ids or []))

        self._registered = True

    async def _deliver(self, message: dict[str, Any], source: str) -> None:
        try:
            await _call(self.on_message, message, source)
        except Exception:  # noqa: BLE001 - one bad message must not stop the listener
            logger.exception("failed to ingest message %s", message.get("message_id"))

    async def backfill(self) -> int:
        """Fetch the last N messages per channel, oldest first, and hand
        every one to the ingest callback; already-stored messages are
        ignored there (FR-004)."""
        limit = self.s.telegram_backfill_limit
        if limit <= 0:
            return 0
        total = 0
        for channel, entity in self._entities.items():
            try:
                fetched = [m async for m in self._client.iter_messages(entity, limit=limit)]
            except Exception as exc:  # noqa: BLE001
                logger.warning("backfill failed for %s: %s", channel, type(exc).__name__)
                continue
            for message in reversed(fetched):
                await self._deliver(format_message(message, channel), "backfill")
                total += 1
        logger.info("backfill delivered %d message(s) from %d channel(s)", total, len(self._entities))
        return total

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """First connection. Fails (non-zero exit) only when the session
        is not authorised or no channel resolves."""
        self._client = self._factory(self.s)
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise ConfigError("Telegram session is not authorised; run: botctl telegram-login")
        await self._resolve()
        self._register()
        self.connected = True
        await self.backfill()

    async def run(self, stop: asyncio.Event) -> None:
        """Stay connected until ``stop``. Never returns because Telegram
        is unreachable (FR-006)."""
        backoff = BACKOFF_START_S
        while not stop.is_set():
            waiter = asyncio.ensure_future(stop.wait())
            runner = asyncio.ensure_future(self._client.run_until_disconnected())
            done, _ = await asyncio.wait({waiter, runner}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                runner.cancel()
                break
            waiter.cancel()
            self.connected = False
            if runner.exception() is not None:
                logger.warning("Telegram connection lost: %s", type(runner.exception()).__name__)
            if not self.outage_alerted:
                self.outage_alerted = True
                await _call(self.alert, "Telegram disconnected", "Reconnecting with backoff; signals are backfilled on return.", False)
            while not stop.is_set():
                try:
                    await self._client.connect()
                    if not await self._client.is_user_authorized():
                        raise ConnectionError("session no longer authorised")
                    if not self._entities:
                        await self._resolve()
                    if not self._registered:
                        self._register()
                    break
                except Exception as exc:  # noqa: BLE001
                    delay = backoff * (0.5 + random.random() * 0.5) * self._scale
                    backoff = min(BACKOFF_CAP_S, backoff * 2.0)
                    logger.warning("Telegram reconnect failed (%s); retrying in %.0fs", type(exc).__name__, delay)
                    await wait_event(stop, delay)
            if stop.is_set():
                break
            backoff = BACKOFF_START_S
            self.connected = True
            self.outage_alerted = False
            await _call(self.alert, "Telegram reconnected", "Backfilling recent messages.", False)
            await self.backfill()

    async def disconnect(self) -> None:
        self.connected = False
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    result = fn(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result
