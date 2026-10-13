"""Scenario outlook: the pure rules (docs/outlook.md).

A level-based, session-by-session brief: context, key levels and if/then
scenarios with an invalidation. It is a scenario outlook, not news and
not a signal: nothing here places an order, and nothing here is used by
the trading engine.

Everything in this module is a function of fetched data. No price,
level or probability is invented: what the sources did not supply is
left out and shown as unavailable (§11).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

LABEL = "Scenario outlook, not news"
DISCLAIMER = "Not financial advice. Trading gold with leverage carries a high risk of loss."
PAST_CAPTION = "Past outcomes do not predict future results"

SESSION_ORDER = ("asia", "london", "newyork")
SESSION_NAMES = {"asia": "Asia", "london": "London", "newyork": "New York"}
DEFAULT_SESSIONS: dict[str, tuple[str, str]] = {  # brief time, approximate session open (local to the outlook zone)
    "asia": ("06:45", "07:00"),
    "london": ("13:45", "14:00"),
    "newyork": ("20:15", "20:30"),
}
SOURCE_TYPES = ("technical", "news", "calendar", "macro", "positioning")
OUTCOMES = ("not_triggered", "target", "invalidation", "expired_open")
OUTCOME_TEXT = {
    "not_triggered": "Not triggered",
    "target": "Triggered, hit target",
    "invalidation": "Triggered, hit invalidation",
    "expired_open": "Triggered, expired open",
}
SCENARIO_NAMES = {"A": "Rejection", "B": "Breakout", "C": "Breakdown"}
AGING_AT = 0.75  # share of the validity after which a brief is "aging"
IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}
EVENT_HORIZON_H = 36  # the timeline shows what falls within this many hours of the brief
NEXT_HIGH_HORIZON_D = 21
TEXT_MAX = 200
NOTE_MAX = 2000
MAX_CUSTOM_LEVELS = 20


CANDLE_KINDS = ("pivots", "ranges", "swings", "averages")  # computed from the exchange's own candles
REMOTE_KINDS = ("ics", "fed_json", "calendar_json", "rss", "levels_json")  # fetched from a URL
KIND_TYPES = {"pivots": "technical", "ranges": "technical", "swings": "technical", "averages": "technical", "levels_json": "technical",
              "ics": "calendar", "fed_json": "calendar", "calendar_json": "calendar", "rss": "news"}


@dataclass(frozen=True)
class SourceSpec:
    """One entry of the source registry (§8.1). Sources are configuration:
    one is added, re-weighted or switched off without a code change."""

    id: str
    title: str
    type: str                      # technical | news | calendar | macro | positioning
    kind: str                      # how it is read: one of CANDLE_KINDS or REMOTE_KINDS
    url: Optional[str] = None      # what is fetched
    link: Optional[str] = None     # the page a reader is sent to
    priority: int = 100            # lower comes first
    weight: float = 1.0            # share in a level's weighted price and in the agreement score
    enabled: bool = True
    license: str = ""              # terms of use and attribution
    offset: float = 0.0            # levels_json: added to every price (a feed quoted on another market)


THIRD_PARTY = "Third party. Confirm its terms of use and rate limits before enabling."
DEFAULT_REGISTRY: tuple[dict[str, Any], ...] = (
    {"id": "hl-pivots", "title": "Daily pivot points", "kind": "pivots", "priority": 10, "license": "Computed from Hyperliquid public market data."},
    {"id": "hl-ranges", "title": "Previous day and week range", "kind": "ranges", "priority": 20, "license": "Computed from Hyperliquid public market data."},
    {"id": "hl-swings", "title": "4H and 1H swing points", "kind": "swings", "priority": 30, "license": "Computed from Hyperliquid public market data."},
    {"id": "hl-averages", "title": "1H and 4H moving averages", "kind": "averages", "priority": 40, "weight": 0.8, "license": "Computed from Hyperliquid public market data."},
    {"id": "bls-calendar", "title": "BLS release schedule", "kind": "ics", "priority": 50, "url": "https://www.bls.gov/schedule/news_release/bls.ics",
     "link": "https://www.bls.gov/schedule/news_release/", "license": "US government work, public domain."},
    {"id": "fed-calendar", "title": "Federal Reserve calendar", "kind": "fed_json", "priority": 60, "url": "https://www.federalreserve.gov/json/calendar.json",
     "link": "https://www.federalreserve.gov/newsevents/calendar.htm", "license": "US government work, public domain."},
    {"id": "ff-calendar", "title": "Forex Factory calendar", "kind": "calendar_json", "priority": 70, "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
     "link": "https://www.forexfactory.com/calendar", "enabled": False, "license": THIRD_PARTY},
    {"id": "investing-commodities", "title": "Investing.com commodities news", "kind": "rss", "priority": 80, "url": "https://www.investing.com/rss/news_11.rss",
     "link": "https://www.investing.com/news/commodities-news", "enabled": False, "license": THIRD_PARTY + " Headlines and links only."},
)


def build_registry(extra: Sequence[Mapping[str, Any]] = (), enable: Sequence[str] = (), disable: Sequence[str] = ()) -> tuple[SourceSpec, ...]:
    """The built-in sources with the operator's file laid over them (an
    entry with a known id changes that source, a new id adds one), then
    the enable and disable lists. Raises ValueError naming what is wrong."""
    merged: dict[str, dict[str, Any]] = {entry["id"]: dict(entry) for entry in DEFAULT_REGISTRY}
    fields = set(SourceSpec.__dataclass_fields__)
    for entry in extra:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            raise ValueError("every source needs an id")
        unknown = set(entry) - fields
        if unknown:
            raise ValueError(f"source {entry['id']}: unknown field(s) {', '.join(sorted(unknown))}")
        merged.setdefault(entry["id"], {}).update(entry)
    for name, wanted in [(n, True) for n in enable] + [(n, False) for n in disable]:
        if name not in merged:
            raise ValueError(f"unknown source {name!r} (known: {', '.join(sorted(merged))})")
        merged[name]["enabled"] = wanted
    specs = []
    for source_id, entry in merged.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", source_id):
            raise ValueError(f"source id {source_id!r} must be lower-case letters, digits and dashes")
        kind = entry.get("kind")
        if kind not in KIND_TYPES:
            raise ValueError(f"source {source_id}: kind must be one of {', '.join(KIND_TYPES)}")
        entry.setdefault("type", KIND_TYPES[kind])
        entry.setdefault("title", source_id)
        if entry["type"] not in SOURCE_TYPES:
            raise ValueError(f"source {source_id}: type must be one of {', '.join(SOURCE_TYPES)}")
        for key in ("url", "link"):
            value = entry.get(key)
            if value is not None and not (isinstance(value, str) and value.startswith("https://")):
                raise ValueError(f"source {source_id}: {key} must be an https:// address")
        if kind in REMOTE_KINDS and not entry.get("url"):
            raise ValueError(f"source {source_id}: a {kind} source needs a url")
        try:
            spec = SourceSpec(**{**entry, "priority": int(entry.get("priority", 100)), "weight": float(entry.get("weight", 1.0)),
                                 "offset": float(entry.get("offset", 0.0)), "enabled": bool(entry.get("enabled", True))})
        except (TypeError, ValueError) as exc:
            raise ValueError(f"source {source_id}: {exc}") from exc
        if not 0 < spec.weight <= 10:
            raise ValueError(f"source {source_id}: weight must be above 0 and at most 10")
        specs.append(spec)
    return tuple(sorted(specs, key=lambda spec: (spec.priority, spec.id)))


@dataclass(frozen=True)
class OutlookConfig:
    enabled: bool = False  # load_settings turns it on; a Settings built in code starts nothing that reaches the network
    coin: str = "xyz:GOLD"
    instrument: str = "XAUUSD"
    sessions: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_SESSIONS))
    tz_name: str = "WIB"
    tz_offset_minutes: int = 420
    confluence_tolerance: float = 5.0   # levels from different sources within this distance are one level
    carry_distance: float = 50.0        # a user's level further than this from price is archived at the next brief
    min_level_sources: int = 2          # fewer level sources than this: the brief is marked low confidence
    history_days: int = 365             # 0 keeps every brief
    no_trade_before_min: int = 15
    no_trade_after_min: int = 15
    event_refresh_delay_min: int = 5    # a high-impact release makes the brief stale; the refresh follows this much later
    manual_refresh_cooldown_s: int = 300
    sources: tuple[SourceSpec, ...] = field(default_factory=build_registry)

    @property
    def tz(self) -> timedelta:
        return timedelta(minutes=self.tz_offset_minutes)

    @property
    def display(self) -> str:
        return f"{self.instrument[:3]}/{self.instrument[3:]}" if len(self.instrument) == 6 else self.instrument


# ---------------------------------------------------------------------- time
def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def local_text(moment: datetime, cfg: OutlookConfig, *, date: bool = True) -> str:
    """``Wed 14 Oct 19:30 WIB``: every time a reader sees is in the outlook zone."""
    local = moment.astimezone(UTC) + cfg.tz
    return local.strftime("%a %d %b %H:%M" if date else "%H:%M") + " " + cfg.tz_name


def eastern_to_utc(naive: datetime) -> datetime:
    """US Eastern wall time to UTC. Daylight time runs from the second
    Sunday of March to the first Sunday of November, 02:00 local."""

    def nth_sunday(month: int, nth: int) -> datetime:
        first = datetime(naive.year, month, 1, 2)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))

    daylight = nth_sunday(3, 2) <= naive < nth_sunday(11, 1)
    return (naive + timedelta(hours=4 if daylight else 5)).replace(tzinfo=UTC)


@dataclass(frozen=True)
class Slot:
    """One scheduled brief: generated at ``start``, valid until ``end``
    (the next brief in the daily cycle)."""

    session: str
    local_date: str
    start: datetime
    opens: datetime
    end: datetime

    @property
    def id(self) -> str:
        return f"{self.local_date}-{self.session}"


def parse_clock(text: str) -> int:
    """``06:45`` -> minutes after local midnight."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip())
    if not match:
        raise ValueError(f"{text!r} is not a time of day (HH:MM)")
    return int(match.group(1)) * 60 + int(match.group(2))


def check_sessions(sessions: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, str]]:
    """Session times as configured: all three sessions, in order through the day."""
    if set(sessions) != set(SESSION_ORDER):
        raise ValueError(f"give exactly the sessions {', '.join(SESSION_ORDER)}")
    clean: dict[str, tuple[str, str]] = {}
    previous = -1
    for name in SESSION_ORDER:
        value = sessions[name]
        if isinstance(value, str) or len(value) != 2:
            raise ValueError(f"{name} must be [brief time, session open], e.g. [\"06:45\", \"07:00\"]")
        brief, opens = parse_clock(str(value[0])), parse_clock(str(value[1]))
        if brief <= previous:
            raise ValueError("brief times must rise through the day: asia, london, newyork")
        if opens < brief:
            raise ValueError(f"{name} opens before its brief")
        previous = brief
        clean[name] = (str(value[0]).strip(), str(value[1]).strip())
    return clean


def _slots_on(cfg: OutlookConfig, local_day: datetime) -> list[Slot]:
    """The three briefs of one local calendar day (a naive local midnight)."""
    starts = [local_day + timedelta(minutes=parse_clock(cfg.sessions[name][0])) for name in SESSION_ORDER]
    ends = starts[1:] + [starts[0] + timedelta(days=1)]
    slots = []
    for name, start, end in zip(SESSION_ORDER, starts, ends, strict=True):
        opens = local_day + timedelta(minutes=parse_clock(cfg.sessions[name][1]))
        slots.append(
            Slot(name, local_day.strftime("%Y-%m-%d"), (start - cfg.tz).replace(tzinfo=UTC), (opens - cfg.tz).replace(tzinfo=UTC), (end - cfg.tz).replace(tzinfo=UTC))
        )
    return slots


def _local_midnight(cfg: OutlookConfig, now: datetime) -> datetime:
    local = now.astimezone(UTC).replace(tzinfo=None) + cfg.tz
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def current_slot(cfg: OutlookConfig, now: datetime) -> Optional[Slot]:
    """The brief that covers ``now``, or None between Friday's last brief
    running out and Monday's first (briefs run Monday to Friday)."""
    day = _local_midnight(cfg, now)
    for offset in (0, -1):
        local_day = day + timedelta(days=offset)
        if local_day.weekday() >= 5:
            continue
        for slot in reversed(_slots_on(cfg, local_day)):
            if slot.start <= now < slot.end:
                return slot
    return None


def next_slot(cfg: OutlookConfig, now: datetime) -> Slot:
    day = _local_midnight(cfg, now)
    for offset in range(0, 8):
        local_day = day + timedelta(days=offset)
        if local_day.weekday() >= 5:
            continue
        for slot in _slots_on(cfg, local_day):
            if slot.start > now:
                return slot
    raise AssertionError("a week always holds a weekday")  # pragma: no cover


def week_start(moment: datetime, cfg: OutlookConfig) -> str:
    """The Monday (local date) of the week a moment falls in."""
    local = moment.astimezone(UTC).replace(tzinfo=None) + cfg.tz
    return (local - timedelta(days=local.weekday())).strftime("%Y-%m-%d")


def status_of(generated_at: datetime, valid_until: datetime, stale_at: Optional[datetime], now: datetime) -> str:
    if stale_at is not None or now >= valid_until:
        return "stale"
    span = (valid_until - generated_at).total_seconds()
    return "aging" if span > 0 and (now - generated_at).total_seconds() / span >= AGING_AT else "fresh"


# ---------------------------------------------------------------------- prices
def decimals_for(price: float) -> int:
    return 1 if abs(price) >= 1000 else 2 if abs(price) >= 10 else 4


def round_px(value: float, reference: Optional[float] = None) -> float:
    return round(value, decimals_for(reference if reference is not None else value))


def px(value: float) -> str:
    """A price as text: thousands separators, no trailing zeros."""
    text = f"{value:,.{decimals_for(value)}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------- technical readings from candles
@dataclass(frozen=True)
class RawLevel:
    price: float
    role: str
    timeframe: str


@dataclass(frozen=True)
class Lean:
    direction: str  # bullish | bearish | neutral
    reason: str


@dataclass(frozen=True)
class Reading:
    """What one technical source says: its levels and which way it leans."""

    levels: tuple[RawLevel, ...] = ()
    lean: Optional[Lean] = None


def closed_candles(candles: Iterable[Any], now_ms: int) -> list[Any]:
    return sorted((c for c in candles if c.close_ms < now_ms), key=lambda c: c.open_ms)


def sma(values: Sequence[float], length: int) -> Optional[float]:
    return sum(values[-length:]) / length if len(values) >= length else None


def atr(candles: Sequence[Any], length: int = 14) -> Optional[float]:
    """Average true range of the last ``length`` closed candles."""
    if len(candles) < length + 1:
        return None
    ranges = []
    for previous, candle in zip(candles[-length - 1 : -1], candles[-length:], strict=True):
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return sum(ranges) / length


def pivot_reading(daily: Sequence[Any], price: float) -> Reading:
    """Classic floor pivots from the last completed daily candle."""
    if not daily:
        return Reading()
    day = daily[-1]
    pivot = (day.high + day.low + day.close) / 3
    span = day.high - day.low
    levels = [
        RawLevel(pivot, "Daily pivot", "1D"),
        RawLevel(2 * pivot - day.low, "Pivot R1", "1D"),
        RawLevel(pivot + span, "Pivot R2", "1D"),
        RawLevel(day.high + 2 * (pivot - day.low), "Pivot R3", "1D"),
        RawLevel(2 * pivot - day.high, "Pivot S1", "1D"),
        RawLevel(pivot - span, "Pivot S2", "1D"),
        RawLevel(day.low - 2 * (day.high - pivot), "Pivot S3", "1D"),
    ]
    edge = pivot * 0.0005
    if price > pivot + edge:
        lean = Lean("bullish", f"Price holds above the daily pivot ({px(round_px(pivot))}).")
    elif price < pivot - edge:
        lean = Lean("bearish", f"Price trades below the daily pivot ({px(round_px(pivot))}).")
    else:
        lean = Lean("neutral", f"Price sits on the daily pivot ({px(round_px(pivot))}).")
    return Reading(tuple(levels), lean)


def range_reading(daily: Sequence[Any], price: float) -> Reading:
    """Previous day's and previous week's range."""
    if not daily:
        return Reading()
    day = daily[-1]
    levels = [RawLevel(day.high, "Previous day high", "1D"), RawLevel(day.low, "Previous day low", "1D"), RawLevel(day.close, "Previous day close", "1D")]
    weeks: dict[tuple[int, int], list[Any]] = {}
    for candle in daily:
        stamp = datetime.fromtimestamp(candle.open_ms / 1000, UTC).isocalendar()
        weeks.setdefault((stamp[0], stamp[1]), []).append(candle)
    ordered = sorted(weeks)
    # the last key is the week still running (or just ended); the one before it is complete if it has its days
    if len(ordered) >= 2 and len(weeks[ordered[-2]]) >= 4:
        week = weeks[ordered[-2]]
        levels += [RawLevel(max(c.high for c in week), "Previous week high", "1W"), RawLevel(min(c.low for c in week), "Previous week low", "1W")]
    if price > day.high:
        lean = Lean("bullish", f"Price is above the previous day's high ({px(round_px(day.high))}).")
    elif price < day.low:
        lean = Lean("bearish", f"Price is below the previous day's low ({px(round_px(day.low))}).")
    else:
        lean = Lean("neutral", f"Price is inside the previous day's range ({px(round_px(day.low))}–{px(round_px(day.high))}).")
    return Reading(tuple(levels), lean)


def _swings(candles: Sequence[Any], wing: int = 2) -> tuple[list[float], list[float]]:
    """Fractal swing highs and lows, oldest first: a bar whose high (low)
    is beyond the ``wing`` bars on each side."""
    highs: list[float] = []
    lows: list[float] = []
    for index in range(wing, len(candles) - wing):
        window = candles[index - wing : index + wing + 1]
        candle = candles[index]
        if candle.high == max(c.high for c in window) and sum(1 for c in window if c.high == candle.high) == 1:
            highs.append(candle.high)
        if candle.low == min(c.low for c in window) and sum(1 for c in window if c.low == candle.low) == 1:
            lows.append(candle.low)
    return highs, lows


def swing_reading(four_hour: Sequence[Any], one_hour: Sequence[Any]) -> Reading:
    """Recent swing highs and lows; the lean is the 4H structure."""
    levels: list[RawLevel] = []
    highs_4h, lows_4h = _swings(four_hour[-60:])
    highs_1h, lows_1h = _swings(one_hour[-72:])
    levels += [RawLevel(value, "Swing high", "4H") for value in highs_4h[-3:]]
    levels += [RawLevel(value, "Swing low", "4H") for value in lows_4h[-3:]]
    levels += [RawLevel(value, "Swing high", "1H") for value in highs_1h[-2:]]
    levels += [RawLevel(value, "Swing low", "1H") for value in lows_1h[-2:]]
    lean: Optional[Lean] = None
    if len(highs_4h) >= 2 and len(lows_4h) >= 2:
        rising_highs, rising_lows = highs_4h[-1] > highs_4h[-2], lows_4h[-1] > lows_4h[-2]
        if rising_highs and rising_lows:
            lean = Lean("bullish", "4H structure makes higher highs and higher lows.")
        elif not rising_highs and not rising_lows:
            lean = Lean("bearish", "4H structure makes lower highs and lower lows.")
        else:
            lean = Lean("neutral", "4H structure is sideways: swing highs and lows do not agree.")
    return Reading(tuple(levels), lean)


def average_reading(one_hour: Sequence[Any], four_hour: Sequence[Any], price: float) -> Reading:
    """Simple moving averages as dynamic levels; the lean is price against the 50-period averages."""
    levels: list[RawLevel] = []
    anchors: dict[str, float] = {}
    for label, candles in (("1H", one_hour), ("4H", four_hour)):
        closes = [c.close for c in candles]
        for length in (50, 100, 200):
            value = sma(closes, length)
            if value is not None:
                levels.append(RawLevel(value, f"{length} SMA", label))
                if length == 50:
                    anchors[label] = value
    lean: Optional[Lean] = None
    if anchors:
        named = " and ".join(f"{label} 50 SMA ({px(round_px(value))})" for label, value in anchors.items())
        if all(price > value for value in anchors.values()):
            lean = Lean("bullish", f"Price is above the {named}.")
        elif all(price < value for value in anchors.values()):
            lean = Lean("bearish", f"Price is below the {named}.")
        else:
            lean = Lean("neutral", f"Price is between the {named}.")
    return Reading(tuple(levels), lean)


CANDLE_NEEDS = {"1d": 16, "4h": 210, "1h": 210, "15m": 40}  # interval -> candles asked for


def candle_reading(kind: str, candles: Mapping[str, Sequence[Any]], price: float, now_ms: int) -> Reading:
    """One technical source's reading from the exchange's own candles."""
    # weekend candles are left out: their thin range would squeeze Monday's pivots into a few points
    daily = [c for c in closed_candles(candles.get("1d", ()), now_ms) if datetime.fromtimestamp(c.open_ms / 1000, UTC).weekday() < 5]
    four_hour = closed_candles(candles.get("4h", ()), now_ms)
    one_hour = closed_candles(candles.get("1h", ()), now_ms)
    if kind == "pivots":
        return pivot_reading(daily, price)
    if kind == "ranges":
        return range_reading(daily, price)
    if kind == "swings":
        return swing_reading(four_hour, one_hour)
    if kind == "averages":
        return average_reading(one_hour, four_hour, price)
    raise ValueError(f"unknown technical kind {kind!r}")


# ---------------------------------------------------------------------- cross-checking (§8.3)
def cluster_levels(raw: Mapping[str, Sequence[RawLevel]], weights: Mapping[str, float], tolerance: float) -> list[dict[str, Any]]:
    """Levels from every source, merged where they fall within
    ``tolerance`` of each other. A cluster's price is the weighted
    average of its members; two or more sources make it a confluence."""
    members = sorted(((level.price, source, level) for source, levels in raw.items() for level in levels), key=lambda m: m[0])
    groups: list[list[tuple[float, str, RawLevel]]] = []
    for member in members:
        if groups and member[0] - groups[-1][0][0] <= tolerance:  # measured from the cluster's first member: clusters do not chain
            groups[-1].append(member)
        else:
            groups.append([member])
    clusters = []
    for group in groups:
        total = sum(weights.get(source, 1.0) for _, source, _ in group) or 1.0
        price = sum(value * weights.get(source, 1.0) for value, source, _ in group) / total
        sources = list(dict.fromkeys(source for _, source, _ in group))
        roles = list(dict.fromkeys(f"{level.role} ({level.timeframe})" for _, _, level in group))
        frames = list(dict.fromkeys(level.timeframe for _, _, level in group))
        clusters.append(
            {
                "price": round_px(price),
                "role": " + ".join(roles[:3]) + (f" + {len(roles) - 3} more" if len(roles) > 3 else ""),
                "timeframe": "/".join(frames),
                "sources": sources,
                "weight": sum(weights.get(source, 1.0) for source in sources),
                "confluence": len(sources) >= 2,
            }
        )
    return clusters


def select_levels(clusters: Sequence[Mapping[str, Any]], price: float, min_gap: float, per_side: int = 3) -> list[dict[str, Any]]:
    """Up to ``per_side`` resistance and support levels around price.
    Among the nearest candidates a confluence outranks a single-source
    level; a level closer than ``min_gap`` is the price itself, not a level."""
    chosen: list[dict[str, Any]] = []
    for kind, above in (("resistance", True), ("support", False)):
        side = [c for c in clusters if (c["price"] - price if above else price - c["price"]) > min_gap]
        nearest = sorted(side, key=lambda c: abs(c["price"] - price))[: per_side * 2]
        best = sorted(nearest, key=lambda c: (not c["confluence"], -c["weight"], abs(c["price"] - price)))[:per_side]
        chosen += [{**level, "type": kind} for level in best]
    chosen.sort(key=lambda level: -level["price"])
    return [
        {
            "id": f"lvl-{index}", "price": level["price"], "type": level["type"], "role": level["role"], "timeframe": level["timeframe"],
            "origin": "generated", "sources": list(level["sources"]), "sourceCount": len(level["sources"]),
            "confluence": bool(level["confluence"]), "singleSource": not level["confluence"],
        }
        for index, level in enumerate(chosen, 1)
    ]


def reconcile_bias(leans: Mapping[str, Lean], weights: Mapping[str, float]) -> dict[str, Any]:
    """One bias from several sources. Opposite leans are never averaged
    away: the bias is ``mixed`` and both views are listed."""
    if not leans:
        return {"direction": None, "agreement": None, "reason": None, "sources": [], "views": []}
    views: list[dict[str, Any]] = []
    for direction in ("bullish", "bearish", "neutral"):
        sources = sorted((s for s, lean in leans.items() if lean.direction == direction), key=lambda s: -weights.get(s, 1.0))
        if sources:
            views.append({"direction": direction, "reason": leans[sources[0]].reason, "sources": sources, "weight": sum(weights.get(s, 1.0) for s in sources)})
    by: dict[str, dict[str, Any]] = {view["direction"]: view for view in views}
    bulls, bears = by.get("bullish"), by.get("bearish")
    if bulls and bears:
        direction, agreement = "mixed", "mixed"
        reason = f"Sources disagree: {len(bulls['sources'])} lean bullish, {len(bears['sources'])} lean bearish."
        sources = bulls["sources"] + bears["sources"]
    elif bulls or bears:
        view = bulls or bears
        assert view is not None
        direction, agreement = view["direction"], "partial" if "neutral" in by else "aligned"
        reason, sources = view["reason"], view["sources"]
    else:
        direction, agreement, reason, sources = "neutral", "aligned", by["neutral"]["reason"], by["neutral"]["sources"]
    return {"direction": direction, "agreement": agreement, "reason": reason, "sources": sources, "views": [{k: v for k, v in view.items() if k != "weight"} for view in views]}


# ---------------------------------------------------------------------- events
@dataclass(frozen=True)
class RawEvent:
    title: str
    time: datetime
    impact: Optional[str] = None   # the source's own rating, when it gives one
    country: Optional[str] = None


_CHAIR = re.compile(r"\bchair(man|woman)?\b", re.I)
EVENT_RULES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("cpi", "US CPI", "high", re.compile(r"\bconsumer price index\b|\bcpi\b", re.I)),
    ("ppi", "US PPI", "high", re.compile(r"\bproducer price index\b|\bppi\b", re.I)),
    ("nfp", "US jobs report (NFP)", "high", re.compile(r"employment situation|non-?farm", re.I)),
    ("fomc_minutes", "FOMC minutes", "medium", re.compile(r"fomc (meeting )?minutes", re.I)),
    ("fomc_presser", "FOMC press conference", "high", re.compile(r"fomc press conference", re.I)),
    ("fomc", "FOMC rate decision", "high", re.compile(r"fomc meeting|fomc statement|federal funds rate", re.I)),
)
FED_KEYS = ("fomc", "fomc_presser", "fomc_minutes", "fed_chair")


def classify_event(title: str) -> Optional[tuple[str, str, str]]:
    """(key, name, impact) for a release this outlook follows by name."""
    if re.search(r"\badp\b", title, re.I):  # a private payrolls estimate, not the jobs report
        return None
    for key, name, impact, pattern in EVENT_RULES:
        if pattern.search(title):
            return key, name, impact
    if _CHAIR.search(title) and not re.search(r"vice chair", title, re.I) and re.search(r"fed|speech|speaks|testif|testimony|discussion|remarks", title, re.I):
        return "fed_chair", "Fed Chair speaks", "high"
    return None


def reconcile_events(raw: Mapping[str, Sequence[RawEvent]], cfg: OutlookConfig, now: datetime) -> dict[str, Any]:
    """``timeline`` (what falls near this brief), ``nextHigh`` (the next
    high-impact release), ``upcoming`` (everything ahead) and
    ``holidays`` (US holidays today). An event's time is confirmed only
    when two sources give the same one; when they differ, the earlier
    time is shown as a caution."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    holidays: list[str] = []
    for source, events in raw.items():
        for event in events:
            hint = (event.impact or "").lower()
            if hint == "holiday":
                if (event.country or "USD") == "USD" and abs((event.time - now).total_seconds()) <= 18 * 3600:
                    holidays.append(event.title)
                continue
            known = classify_event(event.title)
            if known:
                key, name, impact = known
            elif hint in ("high", "medium") and (event.country or "USD") == "USD":
                key, name, impact = re.sub(r"[^a-z0-9]+", "-", event.title.lower()).strip("-"), event.title.strip()[:80], hint
            else:
                continue
            if hint in IMPACT_RANK and IMPACT_RANK[hint] > IMPACT_RANK[impact]:
                impact = hint
            day = (event.time - timedelta(hours=5)).strftime("%Y-%m-%d")  # the US calendar day, near enough to group on
            entry = grouped.setdefault((key, day), {"key": key, "name": name, "impact": impact, "times": {}})
            if IMPACT_RANK[impact] > IMPACT_RANK[entry["impact"]]:
                entry["impact"] = impact
            entry["times"][source] = min(event.time, entry["times"].get(source, event.time))
    timeline: list[dict[str, Any]] = []
    for entry in grouped.values():
        times = entry["times"]
        earliest, latest = min(times.values()), max(times.values())
        agree = len(times) >= 2 and (latest - earliest).total_seconds() <= 60
        high = entry["impact"] == "high"
        timeline.append(
            {
                "key": entry["key"], "name": entry["name"], "timeUtc": iso(earliest), "impact": entry["impact"],
                "sources": sorted(times), "timeConfirmed": agree,
                "timeNote": None if agree else "one source only" if len(times) == 1 else "sources give different times; the earlier one is shown",
                "noTradeWindowMin": {"before": cfg.no_trade_before_min, "after": cfg.no_trade_after_min} if high else None,
            }
        )
    timeline.sort(key=lambda e: e["timeUtc"])
    upcoming = [e for e in timeline if parse_iso(e["timeUtc"]) >= now - timedelta(hours=1)]
    shown = [e for e in upcoming if parse_iso(e["timeUtc"]) <= now + timedelta(hours=EVENT_HORIZON_H)]
    next_high = next((e for e in upcoming if e["impact"] == "high" and now <= parse_iso(e["timeUtc"]) <= now + timedelta(days=NEXT_HIGH_HORIZON_D)), None)
    return {"timeline": shown, "nextHigh": next_high, "upcoming": [e for e in upcoming if parse_iso(e["timeUtc"]) >= now], "holidays": sorted(set(holidays))}


# ---------------------------------------------------------------------- drivers
@dataclass(frozen=True)
class Headline:
    title: str
    url: str
    published: Optional[datetime] = None


DRIVER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Fed", re.compile(r"\bfed\b|\bfomc\b|powell|rate cut|rate hike|interest rate", re.I)),
    ("USD/yields", re.compile(r"\bdollar\b|\busd\b|\bdxy\b|\byields?\b|treasur", re.I)),
    ("Geopolitics", re.compile(r"geopolit|\bwar\b|ceasefire|truce|sanction|tariff|mideast|middle east|conflict|strait|missile|invasion", re.I)),
    ("Oil", re.compile(r"\boil\b|crude|\bopec\b|brent|\bwti\b", re.I)),
)
MAX_DRIVERS = 3


def build_drivers(upcoming: Sequence[Mapping[str, Any]], headlines: Mapping[str, Sequence[Headline]], cfg: OutlookConfig) -> list[dict[str, Any]]:
    """Up to three drivers, each traceable: a scheduled release from a
    calendar source, or a headline (title and link, never the article)
    from a news source."""
    drivers: list[dict[str, Any]] = []
    fed = next((e for e in upcoming if e["key"] in FED_KEYS and e["impact"] == "high"), None)
    data = next((e for e in upcoming if e["key"] not in FED_KEYS and e["impact"] == "high"), None)
    used: set[str] = set()

    def scheduled(label: str, event: Mapping[str, Any]) -> dict[str, Any]:
        text = f"{event['name']} is due {local_text(parse_iso(event['timeUtc']), cfg)}" + ("." if event["timeConfirmed"] else " (time unconfirmed).")
        return {"label": label, "text": text, "sources": list(event["sources"]), "url": None}

    def headline(label: str, pattern: re.Pattern[str]) -> Optional[dict[str, Any]]:
        found = [(item, source) for source, items in headlines.items() for item in items if pattern.search(item.title)]
        fresh = [pair for pair in found if pair[0].title not in used]  # one headline does not stand for two drivers
        if not fresh:
            return None
        newest, _ = max(fresh, key=lambda pair: pair[0].published or datetime.min.replace(tzinfo=UTC))
        used.add(newest.title)
        sources = list(dict.fromkeys(source for _, source in found))
        return {"label": label, "text": newest.title.strip()[:TEXT_MAX], "sources": sources, "url": newest.url, "headline": True}

    for label, pattern in DRIVER_RULES:
        driver = scheduled("Fed", fed) if label == "Fed" and fed else headline(label, pattern)
        if driver:
            drivers.append(driver)
    if data:
        drivers.append(scheduled("US data", data))
    return drivers[:MAX_DRIVERS]


# ---------------------------------------------------------------------- scenarios
def _rule_text(rule: Mapping[str, Any]) -> str:
    return f"{rule['timeframe']} close {'above' if rule['type'] == 'close_above' else 'below'} {px(rule['price'])}"


def risk_reward(entry_zone: Optional[Sequence[float]], stop: Optional[float], targets: Sequence[float]) -> Optional[float]:
    if not entry_zone or stop is None or not targets:
        return None
    entry = (entry_zone[0] + entry_zone[1]) / 2
    risk = abs(entry - stop)
    return round(abs(targets[0] - entry) / risk, 2) if risk > 0 else None


def _scenario(key: str, direction: str, level: Mapping[str, Any], targets: Sequence[Mapping[str, Any]], band: float, buffer: float, price: float) -> dict[str, Any]:
    long = direction == "long"
    at = level["price"]
    # the zone lies on the side price trades from: in front of the level for a fade, just beyond it for a retest after the break
    zone = [round_px(at, price), round_px(at + band, price)] if long else [round_px(at - band, price), round_px(at, price)]
    if key == "A":
        kind = "support" if long else "resistance"
        trigger_rule = {"type": "touch", "price": zone[1] if long else zone[0], "timeframe": "5m"}
        trigger = f"Rejection candle in the {px(zone[0])}–{px(zone[1])} {kind} zone on the 1–5m chart"
        idea = f"{'Buy' if long else 'Sell'} a failed push into {kind} at {px(at)}; wait for the rejection, do not anticipate it."
        retest = None
    else:
        trigger_rule = {"type": "close_above" if long else "close_below", "price": at, "timeframe": "5m"}
        trigger = _rule_text(trigger_rule)
        idea = f"{'Break above resistance' if long else 'Break below support'} at {px(at)} opens the next level."
        retest = f"A pullback holds {'above' if long else 'below'} {px(at)} (old {'resistance as support' if long else 'support as resistance'})"
    stop = round_px(at - buffer if long else at + buffer, price)
    invalidation_rule = {"type": "close_below" if long else "close_above", "price": at, "timeframe": "5m"}
    target_prices = [t["price"] for t in targets]
    return {
        "key": key, "name": SCENARIO_NAMES[key], "available": True, "direction": direction, "idea": idea,
        "trigger": trigger, "triggerRule": trigger_rule, "retest": retest, "entryZone": zone, "stop": stop, "targets": target_prices,
        "invalidation": _rule_text(invalidation_rule), "invalidationRule": invalidation_rule,
        "riskReward": risk_reward(zone, stop, target_prices), "levelIds": [level["id"]] + [t["id"] for t in targets],
        "sources": list(dict.fromkeys(s for item in [level, *targets] for s in item["sources"])),
        "state": "watching", "outcome": None,
    }


def _missing(key: str, reason: str) -> dict[str, Any]:
    return {"key": key, "name": SCENARIO_NAMES[key], "available": False, "reason": reason, "state": None, "outcome": None}


def build_scenarios(levels: Sequence[Mapping[str, Any]], price: float, bias: Optional[str], atr_15m: Optional[float]) -> list[dict[str, Any]]:
    """Rejection, breakout and breakdown from the selected levels. Zone
    width and stop distance come from the 15-minute average true range,
    so they follow the volatility that was actually measured. A scenario
    whose level or target the sources did not supply is unavailable."""
    if atr_15m is None or atr_15m <= 0:
        return [_missing(key, "No recent 15-minute candles to size the entry zone and stop.") for key in SCENARIO_NAMES]
    band, buffer = round_px(atr_15m, price), round_px(atr_15m / 2, price)
    above = sorted((lv for lv in levels if lv["type"] == "resistance"), key=lambda lv: lv["price"])
    below = sorted((lv for lv in levels if lv["type"] == "support"), key=lambda lv: -lv["price"])

    def beyond(pool: Sequence[Mapping[str, Any]], edge: float, up: bool) -> list[Mapping[str, Any]]:
        return [lv for lv in pool if (lv["price"] > edge if up else lv["price"] < edge)][:2]

    scenarios: list[dict[str, Any]] = []
    # A. rejection: with the bias when there is one, otherwise at whichever level is nearer
    short = bias == "bearish" or (bias != "bullish" and bool(above) and (not below or above[0]["price"] - price <= price - below[0]["price"]))
    level = (above[0] if above else None) if short else (below[0] if below else None)
    if level is None:
        scenarios.append(_missing("A", f"No sourced {'resistance above' if short else 'support below'} price."))
    else:
        targets = beyond(below, level["price"] - band, False) if short else beyond(above, level["price"] + band, True)
        scenarios.append(_scenario("A", "short" if short else "long", level, targets, band, buffer, price) if targets
                         else _missing("A", f"No sourced {'support' if short else 'resistance'} to aim for from {px(level['price'])}."))
    # B. breakout above the nearest resistance, C. breakdown below the nearest support
    for key, pool, up in (("B", above, True), ("C", below, False)):
        if not pool:
            scenarios.append(_missing(key, f"No sourced {'resistance above' if up else 'support below'} price."))
            continue
        level = pool[0]
        targets = beyond(pool[1:], level["price"] + band if up else level["price"] - band, up)
        scenarios.append(_scenario(key, "long" if up else "short", level, targets, band, buffer, price) if targets
                         else _missing(key, f"No sourced level beyond {px(level['price'])} to aim for."))
    return scenarios


# ---------------------------------------------------------------------- risk notes
def risk_notes(events: Sequence[Mapping[str, Any]], holidays: Sequence[str], slot_session: str, cfg: OutlookConfig, sources: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    if holidays:
        notes.append({"kind": "liquidity", "text": f"Thin liquidity: US {holidays[0].lower()} today. Moves can be erratic and levels less reliable.", "sources": list(sources.get("holidays", ()))})
    overlap = {"london": "London runs into the New York open later in this brief: the overlap is usually the most active window.",
               "newyork": "The London–New York overlap falls early in this brief: usually the most active window, then liquidity thins into the US afternoon.",
               "asia": "Asia is usually the quietest session: ranges are narrower and breakouts fail more often."}
    notes.append({"kind": "liquidity", "text": overlap[slot_session], "sources": []})
    high = [e for e in events if e["impact"] == "high"]
    if high:
        names = ", ".join(f"{e['name']} ({local_text(parse_iso(e['timeUtc']), cfg)})" for e in high[:3])
        notes.append({"kind": "spread", "text": f"Spreads widen and slippage rises around {names}. Stops can fill well beyond their level.",
                      "sources": list(dict.fromkeys(s for e in high[:3] for s in e["sources"]))})
    notes.append({"kind": "sizing", "text": "Size the position from the stop distance and risk a small, fixed share of the account on any one scenario.", "sources": []})
    return notes


# ---------------------------------------------------------------------- validation gates (§11)
def scenario_problems(scenario: Mapping[str, Any]) -> dict[str, str]:
    """What is wrong with one scenario's numbers, by field. Used for the
    generated scenarios and, unchanged, for a user's edit."""
    problems: dict[str, str] = {}
    long = scenario.get("direction") == "long"
    zone, stop, targets = scenario.get("entryZone"), scenario.get("stop"), scenario.get("targets") or []
    rule = scenario.get("invalidationRule") or {}
    if not str(scenario.get("trigger") or "").strip():
        problems["trigger"] = "a trigger is required"
    if not zone or len(zone) != 2 or not zone[0] < zone[1]:
        problems["entryZone"] = "the entry zone needs a lower and a higher price"
        return problems
    if not targets:
        problems["targets"] = "at least one target is required"
    elif any((t <= zone[1]) if long else (t >= zone[0]) for t in targets):
        problems["targets"] = f"every target must be {'above' if long else 'below'} the entry zone for a {'long' if long else 'short'}"
    if stop is None:
        problems["stop"] = "a stop is required"
    elif (stop >= zone[0]) if long else (stop <= zone[1]):
        problems["stop"] = f"the stop must be {'below' if long else 'above'} the entry zone for a {'long' if long else 'short'}"
    if rule.get("price") is None:
        problems["invalidation"] = "an invalidation level is required"
    elif (rule["price"] > zone[0]) if long else (rule["price"] < zone[1]):
        problems["invalidation"] = f"the invalidation must be at or {'below' if long else 'above'} the entry zone for a {'long' if long else 'short'}"
    elif stop is not None and "stop" not in problems and ((rule["price"] < stop) if long else (rule["price"] > stop)):
        problems["invalidation"] = "the invalidation must not lie beyond the stop"
    return problems


def outlook_problems(outlook: Mapping[str, Any]) -> list[str]:
    """Why a brief may not be published. Empty: it may."""
    problems: list[str] = []
    known = {source["id"] for source in outlook.get("sources", [])}
    for level in outlook.get("levels", []):
        if not level.get("sources"):
            problems.append(f"level {level.get('price')} has no source")
        if level.get("confluence") != (len(level.get("sources", [])) >= 2) or level.get("singleSource") == level.get("confluence"):
            problems.append(f"level {level.get('price')} is tagged wrongly")
        if set(level.get("sources", [])) - known:
            problems.append(f"level {level.get('price')} cites an unknown source")
    for driver in outlook.get("drivers", []):
        if not driver.get("sources"):
            problems.append(f"driver {driver.get('label')} has no source")
    if not outlook.get("levels"):
        problems.append("no level could be sourced")
    scenarios = [s for s in outlook.get("scenarios", []) if s.get("available")]
    if not scenarios:
        problems.append("no scenario could be built from the sourced levels")
    for scenario in scenarios:
        for name, problem in scenario_problems(scenario).items():
            problems.append(f"scenario {scenario['key']} {name}: {problem}")
    try:
        if parse_iso(outlook["validUntil"]) <= parse_iso(outlook["generatedAt"]):
            problems.append("validUntil is not later than generatedAt")
    except (KeyError, ValueError):
        problems.append("validUntil or generatedAt is missing")
    if (outlook.get("bias") or {}).get("direction") and not outlook["bias"].get("sources"):
        problems.append("the bias has no source")
    if outlook.get("label") != LABEL or not outlook.get("disclaimer"):
        problems.append("the label or the disclaimer is missing")
    return problems


def build_outlook(
    *, cfg: OutlookConfig, slot: Slot, outlook_id: str, trigger: str, now: datetime, price: float, atr_15m: Optional[float],
    sources: Sequence[Mapping[str, Any]], readings: Mapping[str, Reading], events: Mapping[str, Sequence[RawEvent]],
    headlines: Mapping[str, Sequence[Headline]],
) -> dict[str, Any]:
    """The brief, from what the sources returned. ``sources`` is the
    registry view (one entry per enabled source, with whether it answered)."""
    weights = {s["id"]: float(s.get("weight", 1.0)) for s in sources}
    raw_levels = {source: reading.levels for source, reading in readings.items() if reading.levels}
    min_gap = max((atr_15m or 0.0) / 2, cfg.confluence_tolerance / 10)
    levels = select_levels(cluster_levels(raw_levels, weights, cfg.confluence_tolerance), price, min_gap)
    bias = reconcile_bias({source: reading.lean for source, reading in readings.items() if reading.lean}, weights)
    calendar = reconcile_events(events, cfg, now)
    timeline, next_high, holidays = calendar["timeline"], calendar["nextHigh"], calendar["holidays"]
    holiday_sources = [source for source, items in events.items() if any((e.impact or "").lower() == "holiday" for e in items)]
    answered = [s for s in sources if s.get("ok")]
    level_sources = [s["id"] for s in answered if s["id"] in raw_levels]
    return {
        "id": outlook_id, "instrument": cfg.instrument, "display": cfg.display, "coin": cfg.coin, "session": slot.session,
        "sessionName": SESSION_NAMES[slot.session], "trigger": trigger, "generatedAt": iso(now), "validUntil": iso(slot.end),
        "sessionOpensAt": iso(slot.opens), "price": {"value": round_px(price), "at": iso(now)},
        "atr15m": round_px(atr_15m, price) if atr_15m else None,
        "bias": bias, "drivers": build_drivers(calendar["upcoming"], headlines, cfg), "levels": levels,
        "scenarios": build_scenarios(levels, price, bias["direction"], atr_15m),
        "events": timeline, "nextHighImpact": next_high,
        "riskNotes": risk_notes(timeline, holidays, slot.session, cfg, {"holidays": holiday_sources}),
        "flags": {"lowConfidence": len(level_sources) < cfg.min_level_sources, "thinLiquidity": bool(holidays)},
        "sourcesAvailable": len(answered), "sourcesTotal": len(sources), "levelSourcesAvailable": len(level_sources),
        "sources": [dict(s) for s in sources], "label": LABEL, "disclaimer": DISCLAIMER,
        "tz": {"name": cfg.tz_name, "offsetMinutes": cfg.tz_offset_minutes},
        "settings": {"confluenceTolerance": cfg.confluence_tolerance, "carryDistance": cfg.carry_distance, "minLevelSources": cfg.min_level_sources},
    }


# ---------------------------------------------------------------------- outcomes
def evaluate(scenario: Mapping[str, Any], candles: Sequence[Any], start_ms: int, end_ms: int, now_ms: int) -> dict[str, Any]:
    """Where a scenario stands, from the 5-minute candles since the
    brief. Stops and targets are met by a candle's range; close-based
    rules only by a candle that has closed. Within one candle the
    adverse reading wins: a bar that reaches both the stop and the
    target counts as invalidated."""
    result: dict[str, Any] = {"state": "watching", "outcome": None, "triggeredAt": None, "resolvedAt": None}
    if not scenario.get("available", True):
        return {**result, "state": None}
    long = scenario["direction"] == "long"
    trigger, invalid = scenario["triggerRule"], scenario["invalidationRule"]
    stop, targets = scenario.get("stop"), scenario.get("targets") or []
    triggered = False
    for candle in sorted(candles, key=lambda c: c.open_ms):
        if candle.open_ms < start_ms or candle.open_ms >= end_ms:  # a candle already open at the brief holds prices from before it
            continue
        done = candle.close_ms < now_ms
        fresh = False
        if not triggered:
            if trigger["type"] == "touch":
                triggered = candle.low <= trigger["price"] if long else candle.high >= trigger["price"]
            elif done:
                triggered = candle.close > trigger["price"] if trigger["type"] == "close_above" else candle.close < trigger["price"]
                if triggered:  # entered on this close: nothing else in this candle counts
                    result.update(state="triggered", triggeredAt=candle.close_ms)
                    continue
            if not triggered:
                continue
            fresh = True
            result.update(state="triggered", triggeredAt=candle.open_ms)
        stopped = stop is not None and (candle.low <= stop if long else candle.high >= stop)
        closed_out = done and (candle.close < invalid["price"] if invalid["type"] == "close_below" else candle.close > invalid["price"])
        if stopped or closed_out:
            return {**result, "state": "invalidated", "outcome": "invalidation", "resolvedAt": candle.close_ms if done else candle.open_ms}
        if not fresh and targets and (candle.high >= targets[0] if long else candle.low <= targets[0]):
            return {**result, "state": "target", "outcome": "target", "resolvedAt": candle.close_ms if done else candle.open_ms}
    if now_ms >= end_ms:
        result["outcome"] = "expired_open" if triggered else "not_triggered"
    return result


# ---------------------------------------------------------------------- the user's own version (§5.7)
def _number(value: Any, name: str, errors: dict[str, str]) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")) or value <= 0:
        errors[name] = "give a positive number"
        return None
    return float(value)


def clean_levels(items: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """A user's own levels as sent by the page: validated, never trusted."""
    errors: dict[str, str] = {}
    clean: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return [], {"customLevels": "must be a list"}
    if len(items) > MAX_CUSTOM_LEVELS:
        return [], {"customLevels": f"at most {MAX_CUSTOM_LEVELS} levels"}
    for index, item in enumerate(items):
        name = f"customLevels[{index}]"
        if not isinstance(item, dict):
            errors[name] = "must be an object"
            continue
        price = _number(item.get("price"), name + ".price", errors)
        kind = item.get("type")
        if kind not in ("support", "resistance"):
            errors[name + ".type"] = "must be support or resistance"
        note = item.get("note") or ""
        if not isinstance(note, str) or len(note) > TEXT_MAX:
            errors[name + ".note"] = f"at most {TEXT_MAX} characters"
        level_id = item.get("id")
        if level_id is not None and (isinstance(level_id, bool) or not isinstance(level_id, int)):
            errors[name + ".id"] = "must be the id the bot gave this level"
        if price is not None and not any(key.startswith(name) for key in errors):
            clean.append({"id": level_id, "price": price, "type": kind, "note": note.strip()})
    return clean, errors


def apply_edit(generated: Mapping[str, Any], edit: Any) -> tuple[Optional[dict[str, Any]], dict[str, str]]:
    """A user's edit laid over the generated scenario: (the edited
    scenario, errors by field). The direction and the kind of rule are
    the scenario's own; the numbers and the trigger text are the user's.
    The same checks apply as to a generated scenario."""
    errors: dict[str, str] = {}
    if not isinstance(edit, dict):
        return None, {"edit": "must be an object"}
    if not generated.get("available"):
        return None, {"edit": "this scenario has nothing to edit"}
    unknown = set(edit) - {"key", "trigger", "entryZone", "stop", "targets", "invalidation", "editedAt"}
    if unknown:
        return None, {"edit": f"unknown field(s): {', '.join(sorted(unknown))}"}
    long = generated["direction"] == "long"
    trigger = edit.get("trigger", generated["trigger"])
    if not isinstance(trigger, str) or not trigger.strip() or len(trigger) > TEXT_MAX:
        errors["trigger"] = f"a trigger of at most {TEXT_MAX} characters is required"
    zone_in = edit.get("entryZone", generated["entryZone"])
    zone: Optional[list[float]] = None
    if not isinstance(zone_in, list) or len(zone_in) != 2:
        errors["entryZone"] = "give the lower and the higher price of the zone"
    else:
        low, high = _number(zone_in[0], "entryZone", errors), _number(zone_in[1], "entryZone", errors)
        if low is not None and high is not None:
            zone = [low, high]
    stop = _number(edit.get("stop", generated["stop"]), "stop", errors)
    targets_in = edit.get("targets", generated["targets"])
    targets: list[float] = []
    if not isinstance(targets_in, list) or not 1 <= len(targets_in) <= 3:
        errors["targets"] = "give one to three targets"
    else:
        for value in targets_in:
            target = _number(value, "targets", errors)
            if target is not None:
                targets.append(target)
        targets.sort(reverse=not long)
    invalidation = _number(edit.get("invalidation", generated["invalidationRule"]["price"]), "invalidation", errors)
    if errors or zone is None or stop is None or invalidation is None:
        return None, errors
    trigger_rule = dict(generated["triggerRule"])
    trigger_rule["price"] = (zone[1] if long else zone[0]) if trigger_rule["type"] == "touch" else (zone[0] if long else zone[1])
    invalidation_rule = {**generated["invalidationRule"], "price": invalidation}
    edited = {
        **generated, "trigger": trigger.strip(), "triggerRule": trigger_rule, "entryZone": zone, "stop": stop, "targets": targets,
        "invalidation": _rule_text(invalidation_rule), "invalidationRule": invalidation_rule, "riskReward": risk_reward(zone, stop, targets),
        "edited": True, "state": "watching", "outcome": None,
    }
    return (None, problems) if (problems := scenario_problems(edited)) else (edited, {})


# ---------------------------------------------------------------------- review of past briefs (§5.8)
def summarise(rows: Sequence[Mapping[str, Any]], cfg: OutlookConfig) -> dict[str, Any]:
    """Counts per outcome by session, by scenario and by week. ``rows``:
    one per finished scenario with session, key, outcome, generatedAt.
    Counts only: no win rate is derived from them."""

    def table(key_of: Any, order: Optional[Sequence[str]] = None) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, int]] = {}
        for row in rows:
            groups.setdefault(key_of(row), dict.fromkeys(OUTCOMES, 0))[row["outcome"]] += 1
        keys = [k for k in order if k in groups] if order else sorted(groups, reverse=True)
        return [{"key": key, **groups[key], "total": sum(groups[key].values())} for key in keys]

    return {
        "total": len(rows),
        "bySession": table(lambda r: r["session"], SESSION_ORDER),
        "byScenario": table(lambda r: r["key"], tuple(SCENARIO_NAMES)),
        "byWeek": table(lambda r: week_start(parse_iso(r["generatedAt"]), cfg)),
    }
