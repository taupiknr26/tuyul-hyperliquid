"""Deterministic client order ids (PRD §9.6.4).

``cloid = 0x + first 32 hex chars of SHA-256("{env}|{account}|{signal_key}|{role}|{leg}|{attempt}")``

Deterministic on purpose: a restarted process derives the same id for
the same intent, which is what lets an unknown outcome be resolved by
lookup instead of by a blind retry.
"""

from __future__ import annotations

import hashlib

ROLES = ("ENTRY", "SL", "TP", "CLOSE")


def signal_key(channel_id: int, message_id: int) -> str:
    return f"{channel_id}:{message_id}"


def make_cloid(env: str, account: str, key: str, role: str, leg: int, attempt: int) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown order role: {role!r}")
    material = f"{env}|{account.lower()}|{key}|{role}|{leg}|{attempt}"
    return "0x" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
