"""Raw Telegram message -> parsed record (FR-007, FR-008, FR-009).

Ported from the baseline ``app/signal_parser.py`` (parser 1.1.0). The
classification rules and regexes are unchanged; what changed is that the
channel -> format mapping is configuration (``TELEGRAM_CHANNEL_PARSERS``)
instead of a channel id hard-coded in source (LIM-21), and the file
based ``run_parser`` loop is gone (the database is the durable record).

Never drops a message and never guesses a missing field: a field that
cannot be found is reported in ``parse_errors``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Optional

PARSER_VERSION = "1.2.1"

MESSAGE_TYPES = (
    "signal",
    "tp_hit",
    "tp_all_hit",
    "status_ready",
    "promotional",
    "link",
    "media_only",
    "other",
)

_HASHTAG_RE = re.compile(r"#(\w+)")
_ACTION_RE = re.compile(r"\b(BUY|SELL)\b", re.IGNORECASE)
_ZONE_RE = re.compile(r"Zone\s*:?\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_SL_RE = re.compile(r"SL\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TP_LEVEL_RE = re.compile(r"TP\s*(\d+)\s*:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TP_ANY_LEVEL_RE = re.compile(r"TP\s*\d+\s*:", re.IGNORECASE)
_TP_ALL_HIT_RE = re.compile(r"TP\s*ALL\s*HIT", re.IGNORECASE)
_TP_HIT_RE = re.compile(r"TP\s*(\d+)\s*HIT", re.IGNORECASE)
_PIPS_RE = re.compile(r"(\d+)\+?\s*PIPS", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_PROMOTIONAL_PHRASES = (
    "account management",
    "no risk",
    "recover your",
    "contact admin",
    "double your",
)


def classify(text: str, has_media: bool) -> str:
    """Classify a message into one of the eight observed message types."""
    stripped = (text or "").strip()
    lower = stripped.lower()

    if _ACTION_RE.search(text) and "zone" in lower and _TP_ANY_LEVEL_RE.search(text):
        return "signal"

    if _TP_ALL_HIT_RE.search(text):
        return "tp_all_hit"

    if _TP_HIT_RE.search(text):
        return "tp_hit"

    if stripped.startswith("Ready"):
        return "status_ready"

    if any(phrase in lower for phrase in _PROMOTIONAL_PHRASES):
        return "promotional"

    if _URL_RE.search(text):
        return "link"

    if not stripped and has_media:
        return "media_only"

    return "other"


def _extract_hashtags(text: str) -> list[str]:
    return [f"#{tag}" for tag in _HASHTAG_RE.findall(text)]


def _entry_zone(first: str, second: str, action: Optional[str]) -> dict[str, float]:
    """The two zone bounds as ``low`` / ``high``. A BUY zone is often
    written from the top down ("Zone 4205- 4200"); both bounds are
    stated, so ordering them is not a guess. A SELL zone is taken as
    written."""
    low, high = float(first), float(second)
    if action == "BUY" and low > high:
        low, high = high, low
    return {"low": low, "high": high}


def extract_signal_fields(text: str) -> tuple[dict, list[str]]:
    """Extract structured fields from a "signal" message. Missing fields
    are reported in the returned parse_errors list rather than guessed."""
    parse_errors: list[str] = []
    hashtags = _extract_hashtags(text)

    symbol: Optional[str] = hashtags[0].lstrip("#") if hashtags else None
    if symbol is None:
        parse_errors.append("missing symbol (no hashtag found)")

    action_match = _ACTION_RE.search(text)
    action: Optional[str] = action_match.group(1).upper() if action_match else None
    if action is None:
        parse_errors.append("missing action (no BUY/SELL found)")

    zone_match = _ZONE_RE.search(text)
    entry_zone: Optional[dict[str, float]] = None
    if zone_match:
        entry_zone = _entry_zone(zone_match.group(1), zone_match.group(2), action)
    else:
        parse_errors.append("missing entry_zone (no 'Zone <low>-<high>' found)")

    sl_match = _SL_RE.search(text)
    stop_loss: Optional[float] = float(sl_match.group(1)) if sl_match else None
    if stop_loss is None:
        parse_errors.append("missing stop_loss (no 'SL :' found)")

    tp_matches = _TP_LEVEL_RE.findall(text)
    take_profits: Optional[dict[str, float]] = None
    if tp_matches:
        take_profits = {f"tp{int(n)}": float(v) for n, v in tp_matches}
    else:
        parse_errors.append("missing take_profits (no 'TPn :' found)")

    parsed = {
        "symbol": symbol,
        "action": action,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "hashtags": hashtags,
    }
    return parsed, parse_errors


# ----------------------------------------------------------------------
# Per-channel signal formats
#
# ``default``      "Zone <low>-<high>", "SL : <n>", numbered "TPn : <n>".
# ``entry_limit``  "ENTRY <low> Limit <high>", unnumbered "TP::::<n>"
#                  bullets (a non-numeric "TP::::OPEN" is left out, never
#                  guessed at) and "SL:::<n>".
#
# Which channel uses which format is configuration: see
# configure_channel_parsers() and TELEGRAM_CHANNEL_PARSERS.
# ----------------------------------------------------------------------

_ENTRY_LIMIT_RE = re.compile(
    r"ENTRY\s*:?\s*(\d+(?:\.\d+)?)\s*Limit\s*:?\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
_TP_BULLET_RE = re.compile(r"TP\s*:{2,}\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_SL_BULLET_RE = re.compile(r"SL\s*:{2,}\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _classify_entry_limit_channel(text: str, has_media: bool) -> str:
    if _ACTION_RE.search(text) and _ENTRY_LIMIT_RE.search(text) and _TP_BULLET_RE.search(text):
        return "signal"
    return classify(text, has_media)


def _extract_signal_fields_entry_limit_channel(text: str) -> tuple[dict, list[str]]:
    parse_errors: list[str] = []
    hashtags = _extract_hashtags(text)

    symbol: Optional[str] = hashtags[0].lstrip("#") if hashtags else None
    if symbol is None:
        parse_errors.append("missing symbol (no hashtag found)")

    action_match = _ACTION_RE.search(text)
    action: Optional[str] = action_match.group(1).upper() if action_match else None
    if action is None:
        parse_errors.append("missing action (no BUY/SELL found)")

    entry_match = _ENTRY_LIMIT_RE.search(text)
    entry_zone: Optional[dict[str, float]] = None
    if entry_match:
        entry_zone = _entry_zone(entry_match.group(1), entry_match.group(2), action)
    else:
        parse_errors.append("missing entry_zone (no 'ENTRY <low> Limit <high>' found)")

    sl_match = _SL_BULLET_RE.search(text)
    stop_loss: Optional[float] = float(sl_match.group(1)) if sl_match else None
    if stop_loss is None:
        parse_errors.append("missing stop_loss (no 'SL::: <value>' found)")

    # No per-line numbering in this format (just repeated "TP::::<n>"
    # bullets) -- level = position in the text. A non-numeric bullet
    # (e.g. "TP::::OPEN", meaning "no fixed final target") simply
    # doesn't match \d+ and is left out rather than guessed at.
    tp_values = _TP_BULLET_RE.findall(text)
    take_profits: Optional[dict[str, float]] = None
    if tp_values:
        take_profits = {f"tp{i + 1}": float(v) for i, v in enumerate(tp_values)}
    else:
        parse_errors.append("missing take_profits (no 'TP:::: <value>' lines found)")

    parsed = {
        "symbol": symbol,
        "action": action,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "hashtags": hashtags,
    }
    return parsed, parse_errors


ParserFormat = dict[str, Callable]

PARSER_FORMATS: dict[str, ParserFormat] = {
    "default": {
        "classify": classify,
        "extract_signal_fields": extract_signal_fields,
    },
    "entry_limit": {
        "classify": _classify_entry_limit_channel,
        "extract_signal_fields": _extract_signal_fields_entry_limit_channel,
    },
}

# channel reference (username, invite link or numeric id, as a string) -> format name
_channel_formats: dict[str, str] = {}


def configure_channel_parsers(mapping: Optional[dict[str, str]]) -> None:
    """Replace the channel -> format mapping. Raises ValueError for an
    unknown format name so a typo stops startup instead of silently
    falling back to the default format."""
    cleaned: dict[str, str] = {}
    for channel, fmt in (mapping or {}).items():
        if fmt not in PARSER_FORMATS:
            raise ValueError(
                f"unknown parser format {fmt!r} for channel {channel!r}; "
                f"known formats: {', '.join(sorted(PARSER_FORMATS))}"
            )
        cleaned[str(channel).strip()] = fmt
    _channel_formats.clear()
    _channel_formats.update(cleaned)


def channel_parsers() -> dict[str, str]:
    return dict(_channel_formats)


def _format_for(channel_ref: Optional[object]) -> ParserFormat:
    return PARSER_FORMATS[_channel_formats.get(str(channel_ref or "").strip(), "default")]


def classify_for_channel(channel_username: Optional[str], text: str, has_media: bool) -> str:
    """Like classify(), but uses the format configured for the channel,
    falling back to the default format otherwise."""
    return _format_for(channel_username)["classify"](text, has_media)


def extract_signal_fields_for_channel(channel_username: Optional[str], text: str) -> tuple[dict, list[str]]:
    """Like extract_signal_fields(), but uses the format configured for
    the channel, falling back to the default format otherwise."""
    return _format_for(channel_username)["extract_signal_fields"](text)


def _channel_key(message: dict) -> Optional[str]:
    """The configured key a message's channel matches: its reference as
    configured (username / link / id) first, then its numeric id."""
    for candidate in (message.get("channel_username"), message.get("channel_id")):
        if candidate is not None and str(candidate).strip() in _channel_formats:
            return str(candidate).strip()
    return message.get("channel_username")


def extract_tp_update_fields(text: str, message_type: str) -> tuple[dict, list[str]]:
    """Extract structured fields from a "tp_hit" / "tp_all_hit" message."""
    parse_errors: list[str] = []
    hashtags = _extract_hashtags(text)

    symbol: Optional[str] = hashtags[0].lstrip("#") if hashtags else None
    if symbol is None:
        parse_errors.append("missing symbol (no hashtag found)")

    tp_number: Optional[int] = None
    if message_type == "tp_hit":
        tp_match = _TP_HIT_RE.search(text)
        if tp_match:
            tp_number = int(tp_match.group(1))
        else:
            parse_errors.append("missing tp_number (no 'TP <n> HIT' found)")

    pips_match = _PIPS_RE.search(text)
    pips: Optional[int] = int(pips_match.group(1)) if pips_match else None
    if pips is None:
        parse_errors.append("missing pips (no '<n>+ PIPS' found)")

    parsed = {
        "symbol": symbol,
        "tp_number": tp_number,
        "pips": pips,
    }
    return parsed, parse_errors


def parse_message(message: dict, parsed_at: Optional[str] = None) -> dict:
    """Turn a single raw message dict into one parsed record. Never
    raises on unexpected content and never drops the message."""
    if parsed_at is None:
        parsed_at = datetime.now(UTC).isoformat()

    text = message.get("text") or ""
    has_media = bool(message.get("has_media"))
    channel_username = _channel_key(message)
    message_type = classify_for_channel(channel_username, text, has_media)

    parsed: Optional[dict] = None
    parse_errors: list[str] = []

    if message_type == "signal":
        parsed, parse_errors = extract_signal_fields_for_channel(channel_username, text)
    elif message_type in ("tp_hit", "tp_all_hit"):
        parsed, parse_errors = extract_tp_update_fields(text, message_type)

    return {
        "message_id": message.get("message_id"),
        "channel_id": message.get("channel_id"),
        "channel_username": message.get("channel_username"),
        "date": message.get("date"),
        "reply_to_message_id": message.get("reply_to_message_id"),
        "type": message_type,
        "parsed": parsed,
        "parse_errors": parse_errors,
        "original_text": text,
        "parser_version": PARSER_VERSION,
        "parsed_at": parsed_at,
    }
