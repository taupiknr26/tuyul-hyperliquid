"""Secret redaction for logs, audit payloads and notifications (SR-05,
FR-091, NFR-022).

Two layers: literal secret values registered at startup are replaced
wherever they appear, and patterns catch anything that merely *looks*
like a private key, a bot token or a full account address even if it
was never registered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "***"

_PRIVATE_KEY_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
# No leading word boundary: in a Bot API URL the token follows the letters "bot" directly.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")
_ADDRESS_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

# Values under these keys are never written, whatever they contain.
_SENSITIVE_KEYS = frozenset(
    {"signature", "private_key", "secret", "password", "token", "bot_token", "api_hash", "agent_key", "chat_id"}
)
# 64-hex values under these keys are transaction hashes, not keys.
_HASH_KEYS = frozenset({"hash", "tx_hash", "config_hash"})

_literals: list[str] = []


def register_secrets(values: Iterable[str]) -> None:
    for value in values:
        if value and len(value) >= 6 and value not in _literals:
            _literals.append(value)
    _literals.sort(key=len, reverse=True)


def clear_secrets() -> None:
    _literals.clear()


def redact_text(text: str, *, keep_hashes: bool = False) -> str:
    for literal in _literals:
        if literal in text:
            text = text.replace(literal, REDACTED)
    text = _BOT_TOKEN_RE.sub(REDACTED, text)
    if not keep_hashes:
        text = _PRIVATE_KEY_RE.sub(REDACTED, text)
    return _ADDRESS_RE.sub(lambda m: m.group(0)[:8] + "…", text)


def redact(value: Any, _key: str = "") -> Any:
    """Recursively redact a JSON-like structure."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS and v not in (None, "") else redact(v, str(k).lower()))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _key) for item in value]
    if isinstance(value, str):
        return redact_text(value, keep_hashes=_key in _HASH_KEYS)
    return value
