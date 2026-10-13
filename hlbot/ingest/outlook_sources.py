"""Readers for the scenario outlook's sources (docs/outlook.md §8).

Each enabled registry entry is read once per brief, all of them in
parallel. A source that does not answer is reported as unavailable and
the brief is built from the rest: one source's outage never fails the
others. Remote documents are data, never instructions: they are parsed
into events, headlines and levels with fixed rules, capped in size, and
nothing from them is executed or followed.

Read-only, and nothing here is used for trading decisions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from xml.parsers import expat

import aiohttp

from hlbot import __version__
from hlbot.domain.outlook import (
    CANDLE_KINDS,
    Headline,
    Lean,
    RawEvent,
    RawLevel,
    Reading,
    SourceSpec,
    candle_reading,
    eastern_to_utc,
)

logger = logging.getLogger("hlbot.outlook")

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 12.0
EVENTS_BACK = timedelta(days=2)
EVENTS_AHEAD = timedelta(days=45)
HEADLINE_AGE = timedelta(hours=36)
MAX_HEADLINES = 40
MAX_LEVELS = 40
USER_AGENT = f"hlbot-outlook/{__version__} (personal trading dashboard)"

Fetch = Callable[[str], Awaitable[bytes]]


class SourceError(Exception):
    """A source could not be read. The message is shown beside the source."""


@dataclass
class SourceResult:
    spec: SourceSpec
    ok: bool
    fetched_at: datetime
    error: Optional[str] = None
    reading: Reading = field(default_factory=Reading)
    events: list[RawEvent] = field(default_factory=list)
    headlines: list[Headline] = field(default_factory=list)


# ---------------------------------------------------------------------- parsers
def _text(data: bytes) -> str:
    return data.decode("utf-8-sig", errors="replace")


def _in_window(moment: datetime, now: datetime) -> bool:
    return now - EVENTS_BACK <= moment <= now + EVENTS_AHEAD


def parse_ics(data: bytes, now: datetime) -> list[RawEvent]:
    """Release times from an iCalendar file (the BLS schedule). Times
    are US Eastern unless they end in Z; all-day entries carry no time
    and are left out."""
    lines: list[str] = []
    for line in _text(data).replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]  # a folded line continues the one before
        else:
            lines.append(line)
    events: list[RawEvent] = []
    summary: Optional[str] = None
    start: Optional[datetime] = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            summary, start = None, None
        elif line.startswith("SUMMARY"):
            summary = line.split(":", 1)[-1].replace("\\,", ",").strip()
        elif line.startswith("DTSTART"):
            head, _, value = line.partition(":")
            match = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value.strip())
            if match and "VALUE=DATE" not in head.replace("VALUE=DATE-TIME", ""):
                naive = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
                start = naive.replace(tzinfo=UTC) if match.group(3) else eastern_to_utc(naive)
        elif line == "END:VEVENT" and summary and start and _in_window(start, now):
            events.append(RawEvent(summary[:160], start, None, "USD"))
    return events


_FED_TYPES = {"FOMC", "Speeches", "Testimony"}
_FED_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m", re.I)


def parse_fed_json(data: bytes, now: datetime) -> list[RawEvent]:
    """The Federal Reserve's calendar: FOMC meetings, speeches and
    testimony. Its times are US Eastern; a meeting spanning two days is
    timed on its last day, when the decision is published."""
    try:
        listed = json.loads(_text(data)).get("events", [])
    except (ValueError, AttributeError) as exc:
        raise SourceError("unreadable calendar") from exc
    events: list[RawEvent] = []
    for item in listed if isinstance(listed, list) else []:
        if not isinstance(item, dict) or item.get("type") not in _FED_TYPES:
            continue
        clock = _FED_TIME.search(str(item.get("time") or ""))
        days = re.findall(r"\d{1,2}", str(item.get("days") or ""))
        month = re.fullmatch(r"(\d{4})-(\d{2})", str(item.get("month") or ""))
        if not (clock and days and month):
            continue
        hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "p" else 0)
        try:
            naive = datetime(int(month.group(1)), int(month.group(2)), int(days[-1]), hour, int(clock.group(2)))
        except ValueError:
            continue
        moment = eastern_to_utc(naive)
        if _in_window(moment, now):
            events.append(RawEvent(str(item.get("title") or "")[:160], moment, None, "USD"))
    return events


def parse_calendar_json(data: bytes, now: datetime) -> list[RawEvent]:
    """A calendar aggregator's week: ``[{title, country, date, impact}]``
    with an ISO date that carries its own offset."""
    try:
        listed = json.loads(_text(data))
    except ValueError as exc:
        raise SourceError("unreadable calendar") from exc
    events: list[RawEvent] = []
    for item in listed if isinstance(listed, list) else []:
        if not isinstance(item, dict) or item.get("country") != "USD":
            continue
        try:
            moment = datetime.fromisoformat(str(item.get("date")))
        except ValueError:
            continue
        if moment.tzinfo is None:
            continue
        moment = moment.astimezone(UTC)
        if _in_window(moment, now):
            events.append(RawEvent(str(item.get("title") or "")[:160], moment, str(item.get("impact") or "").lower() or None, "USD"))
    return events


def _feed_items(data: bytes) -> list[dict[str, str]]:
    """The ``<item>`` entries of an RSS document as {child tag: text}.

    Read with the expat parser directly, with document type declarations
    and entities refused by the parser itself: no DTD, no entity
    declaration and no external reference is accepted, which rules out
    entity expansion ("billion laughs") and external entity (XXE)
    attacks. This is what defusedxml does, without adding a dependency
    to the hash-pinned lockfile."""

    def refuse(*args: Any) -> Any:
        raise SourceError("the feed declares a document type or entities, which are not accepted")

    items: list[dict[str, str]] = []
    path: list[str] = []
    text: list[str] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        path.append(name)
        text.clear()
        if name == "item":
            items.append({})

    def end(name: str) -> None:
        if len(path) >= 2 and path[-2] == "item" and items:
            items[-1].setdefault(name, "".join(text))
        path.pop()
        text.clear()

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text.append
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise SourceError("unreadable feed") from exc
    return items


def parse_rss(data: bytes, now: datetime) -> list[Headline]:
    """Headlines and their links from an RSS feed. Only the title and the
    link are kept; the article itself is never fetched or stored."""
    headlines: list[Headline] = []
    for item in _feed_items(data):
        title = " ".join(item.get("title", "").split())
        link = item.get("link", "").strip()
        if not title or not link.startswith("https://"):
            continue
        published: Optional[datetime] = None
        stamp = item.get("pubDate", "").strip()
        if stamp:
            try:
                published = parsedate_to_datetime(stamp)
            except (TypeError, ValueError):
                try:
                    published = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    published = None
            if published is not None and published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
        if published is not None and published < now - HEADLINE_AGE:
            continue
        headlines.append(Headline(title[:200], link[:500], published))
        if len(headlines) >= MAX_HEADLINES:
            break
    return headlines


def parse_levels_json(data: bytes, spec: SourceSpec) -> Reading:
    """Levels from a feed the operator points at:
    ``{"levels": [{"price": 4220, "role": "Fib 23.6%", "timeframe": "4H"}],
    "bias": {"direction": "bearish", "reason": "..."}}``."""
    try:
        body = json.loads(_text(data))
    except ValueError as exc:
        raise SourceError("unreadable levels") from exc
    if not isinstance(body, dict):
        raise SourceError("unreadable levels")
    levels: list[RawLevel] = []
    for item in body.get("levels") or []:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price < 1e9:
            continue
        levels.append(RawLevel(float(price) + spec.offset, str(item.get("role") or "Level")[:60], str(item.get("timeframe") or "–")[:8]))
        if len(levels) >= MAX_LEVELS:
            break
    lean: Optional[Lean] = None
    bias = body.get("bias")
    if isinstance(bias, dict) and bias.get("direction") in ("bullish", "bearish", "neutral"):
        lean = Lean(bias["direction"], str(bias.get("reason") or f"{spec.title} leans {bias['direction']}.")[:200])
    return Reading(tuple(levels), lean)


# ---------------------------------------------------------------------- fetching
class SourceReader:
    """Reads every enabled source for one brief. ``fetch`` is the seam
    the tests use; in production it is an HTTPS GET with a size cap."""

    def __init__(self, fetch: Optional[Fetch] = None):
        self._fetch = fetch or self._http_get
        self._session: Optional[aiohttp.ClientSession] = None

    async def _http_get(self, url: str) -> bytes:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S), headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
            )
        async with self._session.get(url) as response:
            if response.status != 200:
                raise SourceError(f"HTTP {response.status}")
            data = b""
            while len(data) <= MAX_BYTES:  # a stream read may return less than was asked for
                chunk = await response.content.read(MAX_BYTES + 1 - len(data))
                if not chunk:
                    return data
                data += chunk
            raise SourceError("the response is too large")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _one(self, spec: SourceSpec, candles: Mapping[str, Sequence[Any]], candle_error: Optional[str], price: float, now: datetime) -> SourceResult:
        result = SourceResult(spec, False, now)
        try:
            if spec.kind in CANDLE_KINDS:
                if candle_error:
                    raise SourceError(candle_error)
                result.reading = candle_reading(spec.kind, candles, price, int(now.timestamp() * 1000))
                if not result.reading.levels:
                    raise SourceError("not enough candles yet")
            else:
                assert spec.url is not None
                data = await self._fetch(spec.url)
                if spec.kind == "ics":
                    result.events = parse_ics(data, now)
                elif spec.kind == "fed_json":
                    result.events = parse_fed_json(data, now)
                elif spec.kind == "calendar_json":
                    result.events = parse_calendar_json(data, now)
                elif spec.kind == "rss":
                    result.headlines = parse_rss(data, now)
                else:
                    result.reading = parse_levels_json(data, spec)
                    if not result.reading.levels:
                        raise SourceError("no levels in the feed")
            result.ok = True
        except SourceError as exc:
            result.error = str(exc)[:160]
        except TimeoutError:
            result.error = "did not answer in time"
        except aiohttp.ClientError as exc:
            result.error = f"not reachable ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 - a source's failure is a line in the brief, never a crash
            logger.warning("outlook source %s failed: %r", spec.id, exc)
            result.error = "could not be read"
        return result

    async def read_all(self, specs: Sequence[SourceSpec], *, candles: Mapping[str, Sequence[Any]], candle_error: Optional[str], price: float, now: datetime) -> list[SourceResult]:
        return list(await asyncio.gather(*(self._one(spec, candles, candle_error, price, now) for spec in specs)))
