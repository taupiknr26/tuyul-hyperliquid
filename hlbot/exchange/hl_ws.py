"""Hyperliquid WebSocket lifecycle (PRD §9.6.5).

One long-lived connection. The server closes a connection that has not
*sent* anything for 60 s, so the client pings on a timer; the ping
interval is short enough that a quiet market still produces a pong well
inside the stale window. Disconnects happen "periodically and without
announcement": reconnect with capped, jittered backoff and resubscribe.
The caller reconciles over REST after every reconnect rather than
trusting the snapshot alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import aiohttp

from hlbot.exchange.base import StreamHealth
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.exchange.ws")

BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 30.0
MIN_RECONNECT_GAP_S = 2.5  # stays under the 30 new connections / minute cap


def next_backoff(current: float) -> float:
    return min(BACKOFF_CAP_S, max(BACKOFF_START_S, current * 2.0))


def with_jitter(delay: float) -> float:
    return delay * (0.5 + random.random() * 0.5)


class HyperliquidWs:
    def __init__(
        self,
        url: str,
        subscriptions: list[dict[str, Any]],
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_connection: Callable[[bool], Awaitable[None]],
        health: StreamHealth,
        clock: Optional[Clock] = None,
        stale_ms: int = 10000,
        ping_enabled: bool = True,
    ):
        self.url = url
        self.subscriptions = subscriptions
        self.on_message = on_message
        self.on_connection = on_connection
        self.health = health
        self.clock = clock or Clock()
        self.stale_ms = stale_ms
        self.ping_interval_s = max(1.0, min(20.0, stale_ms / 1000.0 / 2.5))
        self.ping_enabled = ping_enabled
        self._stopping = asyncio.Event()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ping_sent: Optional[float] = None

    async def stop(self) -> None:
        self._stopping.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def run(self) -> None:
        backoff = BACKOFF_START_S
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self._stopping.is_set():
                started = self.clock.monotonic()
                try:
                    async with session.ws_connect(self.url, heartbeat=None, max_msg_size=0) as ws:
                        self._ws = ws
                        for subscription in self.subscriptions:
                            await ws.send_json({"method": "subscribe", "subscription": subscription})
                        self.health.connected = True
                        self.health.last_message_ms = self.clock.now_ms()
                        await self.on_connection(True)
                        backoff = BACKOFF_START_S
                        await self._read(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any failure means reconnect
                    logger.warning("websocket error: %s", type(exc).__name__)
                finally:
                    was_connected = self.health.connected
                    self.health.connected = False
                    self._ws = None
                    if was_connected:
                        try:
                            await self.on_connection(False)
                        except Exception:  # noqa: BLE001
                            logger.exception("connection callback failed")
                if self._stopping.is_set():
                    break
                self.health.reconnects += 1
                elapsed = self.clock.monotonic() - started
                delay = max(with_jitter(backoff), MIN_RECONNECT_GAP_S - elapsed, 0.0)
                backoff = next_backoff(backoff)
                await wait_event(self._stopping, delay)

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        pinger = asyncio.get_running_loop().create_task(self._ping(ws))
        try:
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                self.health.last_message_ms = self.clock.now_ms()
                try:
                    message = json.loads(raw.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and message.get("channel") == "pong" and self._ping_sent is not None:
                    self.health.ping_ms = round((self.clock.monotonic() - self._ping_sent) * 1000.0, 1)
                    self._ping_sent = None
                if not isinstance(message, dict) or message.get("channel") in ("pong", "subscriptionResponse"):
                    continue
                try:
                    await self.on_message(message)
                except Exception:  # noqa: BLE001 - one bad message must not drop the stream
                    logger.exception("stream handler failed on channel %s", message.get("channel"))
        finally:
            pinger.cancel()

    async def _ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Heartbeat and stale watchdog: a silent socket is closed so the
        outer loop reconnects."""
        while not ws.closed:
            await asyncio.sleep(self.ping_interval_s)
            if ws.closed:
                return
            age = self.clock.now_ms() - self.health.last_message_ms
            if age > max(2 * self.stale_ms, 3000 * self.ping_interval_s):
                logger.warning("websocket silent for %d ms; reconnecting", age)
                await ws.close()
                return
            if self.ping_enabled:
                try:
                    self._ping_sent = self.clock.monotonic()
                    await ws.send_json({"method": "ping"})
                except Exception:  # noqa: BLE001
                    return
