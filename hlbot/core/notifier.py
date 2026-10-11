"""Telegram notifications through a persistent outbox (FR-092, NFR-013).

Formatting is pure (ported from the baseline ``app/telegram_notifier.py``
and re-based on trades, orders and fills). Delivery reads the outbox in
order and retries with backoff until a message is delivered or 24 hours
old, so nothing is lost across a restart or a Bot API outage (fixes
LIM-10). Every message carries the environment label.

The outbox doubles as the alert history shown by the dashboard: with
notifications disabled, rows are still written and marked not-sent.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Optional

from hlbot.config import Settings
from hlbot.db.database import Database, dumps
from hlbot.domain.planning import parse_iso
from hlbot.redact import redact, redact_text
from hlbot.timeutil import Clock, wait_event

logger = logging.getLogger("hlbot.notifier")

TELEGRAM_BOT_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 15.0
MAX_MESSAGE_CHARS = 4000
MAX_AGE = timedelta(hours=24)
RETRY_START_S = 5.0
RETRY_CAP_S = 300.0


class PermanentSendError(Exception):
    """The Bot API rejected the message itself (HTTP 400): retrying the
    same text can never succeed."""


# ---------------------------------------------------------------------- formatting (pure)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _fmt_price(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else "?"


def _fmt_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _fmt_tps(take_profits: Optional[dict[str, float]]) -> str:
    if not take_profits:
        return "?"
    ordered = sorted(take_profits.items(), key=lambda item: int(item[0][2:]))
    return "  ".join(f"TP{item[0][2:]} <code>{_fmt_price(item[1])}</code>" for item in ordered)


def _tag(label: Optional[str]) -> str:
    return f"<b>[{_esc(label)}]</b> " if label else ""


def format_info_signal(event: dict) -> str:
    """A newly parsed signal, valid or not: the operator sees what the
    channel posted even when it is rejected downstream."""
    zone = event.get("entry_zone") or {}
    lines = [
        f"{_tag(event.get('label'))}ℹ️ <b>New Signal</b>  <code>#{event.get('message_id')}</code>",
        f"Channel  <code>{_esc(event.get('channel') or '?')}</code>",
        "",
        f"<b>{_esc(event.get('symbol') or '?')} {_esc(event.get('action') or '?')}</b>"
        + (f"  →  <code>{_esc(event['coin'])}</code>" if event.get("coin") else ""),
        f"Zone  <code>{_fmt_price(zone.get('low'))} – {_fmt_price(zone.get('high'))}</code>",
        f"SL      <code>{_fmt_price(event.get('stop_loss'))}</code>",
        _fmt_tps(event.get("take_profits")),
        "",
    ]
    if event.get("valid"):
        lines.append("✅ <i>Valid</i>" + (f" — expires {_esc(event['expires_at'])}" if event.get("expires_at") else ""))
    else:
        errors = _esc("; ".join(event.get("errors") or []) or "invalid")
        lines.append(f"⚠️ <b>Invalid</b>\n<i>{errors}</i>")
    return "\n".join(lines)


def format_order_placed(event: dict) -> str:
    """An entry filled and its protection was verified on the exchange."""
    full = not event.get("partial")
    header = "✅ <b>Order Placed</b>" if full else "⚠️ <b>Order Partially Filled</b>"
    tp_line = "  ".join(
        f"TP{leg.get('level')} <code>{leg.get('size'):g}@{_fmt_price(leg.get('price'))}</code>" for leg in event.get("legs") or []
    ) or "?"
    return "\n".join(
        [
            f"{_tag(event.get('label'))}{header}  <code>#{event.get('message_id')}</code>",
            f"Channel  <code>{_esc(event.get('channel') or '?')}</code>",
            "",
            f"<b>{_esc(event.get('coin') or '?')} {_esc(event.get('direction') or '?')}</b> — {event.get('size', 0):g} units",
            f"Signal  <code>{_esc(event.get('content_id') or '?')}</code>",
            f"Fill      <code>{_fmt_price(event.get('avg_entry'))}</code>",
            f"SL        <code>{_fmt_price(event.get('sl'))}</code>  <i>(verified on exchange)</i>",
            "",
            tp_line,
        ]
    )


_OUTCOME_HEADERS = {
    "WIN_ALL": "\U0001f7e2 <b>WIN — ALL TARGETS HIT</b>",
    "WIN_PARTIAL": "\U0001f7e1 <b>WIN — PARTIAL</b>",
    "LOSS_ALL": "\U0001f534 <b>LOSS — STOPPED OUT</b>",
    "MANUAL": "⚪ <b>CLOSED MANUALLY</b>",
    "FLATTENED": "\U0001f7e0 <b>FLATTENED BY BOT</b>",
    "LIQUIDATED": "\U0001f480 <b>LIQUIDATED</b>",
}


def _fmt_closed_leg(leg: dict) -> str:
    role = leg.get("role") or "MANUAL"
    tag = f"TP{leg['level']}" if role == "TP" and leg.get("level") else role
    return (
        f"▪️ {_esc(tag)}  <b>{_fmt_money(leg.get('pnl') or 0.0)}</b>  "
        f"<i>({leg.get('size', 0):g} @ {_fmt_price(leg.get('price'))})</i>"
    )


def format_signal_closed(event: dict) -> str:
    """A trade reached zero position and was classified from its fills."""
    header = _OUTCOME_HEADERS.get(event.get("outcome") or "", "⚪ <b>CLOSED</b>")
    lines = [
        f"{_tag(event.get('label'))}{header}",
        f"<code>{_esc(event.get('content_id') or '?')}</code>",
        f"{_esc(event.get('coin') or '?')} {_esc(event.get('direction') or '?')}",
        f"Channel  <code>{_esc(event.get('channel') or '?')}</code>",
        "",
        f"\U0001f4b0 Net PnL:  <b>{_fmt_money(event.get('net_pnl') or 0.0)}</b>",
        f"<i>realised {_fmt_money(event.get('realized_pnl') or 0.0)}, fees -${abs(event.get('fees') or 0.0):,.2f}, "
        f"funding {_fmt_money(event.get('funding') or 0.0)}</i>",
        "",
    ]
    lines += [_fmt_closed_leg(leg) for leg in event.get("legs") or []]
    return "\n".join(lines)


def format_service_status(event: dict) -> str:
    started = event.get("state") == "started"
    header = "\U0001f7e2 <b>Bot Started</b>" if started else "\U0001f6d1 <b>Bot Stopped</b>"
    lines = [
        f"{_tag(event.get('label'))}{header}",
        f"Mode      <code>{_esc(event.get('mode') or '?')}</code>",
        f"Channel  <code>{_esc(event.get('channel') or '?')}</code>",
    ]
    if event.get("note"):
        lines.append(f"<i>{_esc(event['note'])}</i>")
    if not started and event.get("reason"):
        lines.append(f"Reason    <i>{_esc(event['reason'])}</i>")
    return "\n".join(lines)


def format_daily_target_reached(event: dict) -> str:
    pnl = event.get("today_pnl")
    target = event.get("daily_target_percent")
    return "\n".join(
        [
            f"{_tag(event.get('label'))}\U0001f3af <b>Daily Target Reached</b>",
            f"Today's P/L  <b>{_fmt_money(pnl) if pnl is not None else '?'}</b>"
            + (f"  <i>(target {target:g}%)</i>" if target is not None else ""),
            "",
            "No new trades will open until the next UTC day.",
            f"<i>Just-skipped signal: {_esc(event.get('symbol') or '?')} {_esc(event.get('direction') or '?')} — "
            f"<code>{_esc(event.get('content_id') or '?')}</code></i>",
        ]
    )


def format_signal_skipped(event: dict) -> str:
    """A valid signal that will not be traded: expired, duplicate,
    rejected by risk, or failed. One message, with the reason."""
    titles = {
        "EXPIRED": "⌛ <b>Signal Expired</b>",
        "DUPLICATE": "♻️ <b>Duplicate Signal</b>",
        "REJECTED_RISK": "\U0001f6ab <b>Signal Rejected (risk)</b>",
        "FAILED": "❌ <b>Signal Failed</b>",
    }
    return "\n".join(
        [
            f"{_tag(event.get('label'))}{titles.get(event.get('state') or '', '⚪ <b>Signal Skipped</b>')}  "
            f"<code>#{event.get('message_id')}</code>",
            f"<code>{_esc(event.get('content_id') or '?')}</code>",
            f"<i>{_esc(event.get('reason') or 'no reason recorded')}</i>",
        ]
    )


def format_alert(event: dict) -> str:
    icon = "\U0001f6a8" if event.get("critical") else "⚠️"
    lines = [f"{_tag(event.get('label'))}{icon} <b>{_esc(event.get('title') or 'Alert')}</b>"]
    if event.get("detail"):
        lines.append(_esc(event["detail"]))
    return "\n".join(lines)


_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "info_signal": format_info_signal,
    "order_placed": format_order_placed,
    "signal_closed": format_signal_closed,
    "service_status": format_service_status,
    "daily_target_reached": format_daily_target_reached,
    "signal_skipped": format_signal_skipped,
    "alert": format_alert,
}


def format_notification(event: dict) -> str:
    formatter = _FORMATTERS.get(event.get("kind") or "")
    if formatter is None:
        raise ValueError(f"unknown notification kind: {event.get('kind')!r}")
    return formatter(event)


# ---------------------------------------------------------------------- sending


def send_via_bot_api(bot_token: str, chat_id: str, text: str) -> None:
    """One blocking HTTPS POST to the Bot API ``sendMessage`` method."""
    url = f"{TELEGRAM_BOT_API_BASE}/bot{bot_token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 403):
            raise PermanentSendError(f"Bot API HTTP {exc.code}") from None
        raise RuntimeError(f"Bot API HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        # str(exc) can embed the request URL, which contains the token.
        raise RuntimeError(f"Bot API unreachable: {type(exc.reason).__name__}") from None
    if not payload.get("ok"):
        raise RuntimeError("Telegram Bot API rejected the message")


Sender = Callable[[str], Awaitable[None]]


class Notifier:
    def __init__(self, db: Database, settings: Settings, clock: Optional[Clock] = None, sender: Optional[Sender] = None):
        self.db = db
        self.settings = settings
        self.clock = clock or Clock()
        self.enabled = settings.notify_enabled or sender is not None
        self._sender = sender or self._default_sender
        self._wake = asyncio.Event()

    async def _default_sender(self, text: str) -> None:
        assert self.settings.telegram_bot_token and self.settings.notify_chat_id
        await asyncio.to_thread(
            send_via_bot_api, self.settings.telegram_bot_token, self.settings.notify_chat_id, text
        )

    # ------------------------------------------------------------ enqueue
    def notify(self, kind: str, critical: bool = False, **event: Any) -> int:
        """Format and store one notification. Never raises: a formatting
        problem must not break the trading path."""
        event = {"kind": kind, "label": self.settings.label, "critical": critical, **event}
        try:
            text = redact_text(format_notification(event))[:MAX_MESSAGE_CHARS]
        except Exception:  # noqa: BLE001
            logger.exception("failed to format %s notification", kind)
            text = f"[{self.settings.label}] {kind} (formatting failed; see logs)"
        now = self.clock.iso()
        row_id = self.db.insert(
            "outbox",
            kind=kind,
            critical=1 if critical else 0,
            text=text,
            payload=dumps(redact({k: v for k, v in event.items() if k not in ("label",)})),
            created_at=now,
            next_attempt_at=now,
            dropped_at=None if self.enabled else now,
            last_error=None if self.enabled else "notifications disabled",
        )
        self._wake.set()
        return row_id

    def alert(self, title: str, detail: str = "", critical: bool = True) -> int:
        logger.warning("ALERT%s: %s — %s", " (critical)" if critical else "", title, detail.replace("\n", " | "))
        return self.notify("alert", critical=critical, title=title, detail=detail)

    # ------------------------------------------------------------ deliver
    def pending(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND dropped_at IS NULL", default=0))

    async def deliver_due(self) -> int:
        """Send due rows oldest-first. Stops at the first transient
        failure so messages are never delivered out of order."""
        if not self.enabled:
            return 0
        sent = 0
        now = self.clock.now()
        rows = self.db.all(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND dropped_at IS NULL ORDER BY id LIMIT 50"
        )
        for row in rows:
            if now - parse_iso(row["created_at"]) > MAX_AGE:
                self.db.update("outbox", row["id"], dropped_at=self.clock.iso(), last_error="older than 24 h")
                logger.error("notification %d dropped after 24 h undelivered", row["id"])
                continue
            if parse_iso(row["next_attempt_at"]) > now:
                break
            try:
                await self._sender(row["text"])
            except PermanentSendError as exc:
                self.db.update("outbox", row["id"], dropped_at=self.clock.iso(), attempts=row["attempts"] + 1, last_error=str(exc))
                logger.error("notification %d rejected by the Bot API and dropped", row["id"])
                continue
            except Exception as exc:  # noqa: BLE001 - network, 5xx, 429: retry later
                attempts = row["attempts"] + 1
                delay = min(RETRY_CAP_S, RETRY_START_S * (2 ** min(attempts - 1, 8)))
                self.db.update(
                    "outbox",
                    row["id"],
                    attempts=attempts,
                    next_attempt_at=(now + timedelta(seconds=delay)).isoformat(timespec="milliseconds"),
                    last_error=redact_text(str(exc))[:200],
                )
                logger.warning("notification %d not delivered (attempt %d); retry in %.0fs", row["id"], attempts, delay)
                break
            self.db.update("outbox", row["id"], sent_at=self.clock.iso(), attempts=row["attempts"] + 1, last_error=None)
            sent += 1
        return sent

    async def run(self, stop: asyncio.Event, interval_s: float = 2.0) -> None:
        while not stop.is_set():
            try:
                await self.deliver_due()
            except Exception:  # noqa: BLE001
                logger.exception("outbox delivery failed")
            self._wake.clear()
            await wait_event(self._wake, interval_s)

    async def flush(self, timeout_s: float = 10.0) -> None:
        """Best effort at shutdown so the "stopped" message goes out."""
        deadline = self.clock.monotonic() + timeout_s
        while self.enabled and self.pending() and self.clock.monotonic() < deadline:
            if await self.deliver_due() == 0:
                await asyncio.sleep(0.5)

    def recent(self, limit: int = 30, kind: Optional[str] = None) -> list[dict[str, Any]]:
        if kind:
            return self.db.all("SELECT * FROM outbox WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self.db.all("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
