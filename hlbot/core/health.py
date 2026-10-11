"""Health model, automatic pause conditions and the kill switch state
(FR-043, FR-045, FR-046, FR-071; PRD §10.7, §10.8).

Two persisted inputs decide whether a new entry may be sent:

* the **operator control state** (``RUNNING`` / ``PAUSED``), changed only
  by ``pause`` / ``resume`` / ``kill``;
* a set of **conditions** raised automatically. Each has a scope
  (``global`` or one coin), a level and says whether it clears itself.

Protective actions never consult this module: cancels, reduce-only
closes and stop-loss placement are always allowed (FR-046).

``HEALTHY``   nothing blocks entries
``DEGRADED``  entries blocked on an asset, or by a self-clearing condition
``PAUSED``    operator pause, or a global pause-level condition
``HALTED``    a halt-level condition: manual intervention required
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

from hlbot.db.database import Database
from hlbot.timeutil import Clock

logger = logging.getLogger("hlbot.health")

GLOBAL = "global"
BLOCK, PAUSE, HALT = "block", "pause", "halt"
_RANK = {BLOCK: 1, PAUSE: 2, HALT: 3}


class Health:
    def __init__(self, db: Database, clock: Optional[Clock] = None):
        self.db = db
        self.clock = clock or Clock()
        self.alert: Callable[..., Any] | None = None  # set by the notifier wiring
        self._last_state: Optional[str] = None

    # ------------------------------------------------------------ operator control
    def control(self) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM control_state WHERE id = 1")
        assert row is not None
        return row

    def pause(self, reason: str = "operator", by: str = "operator") -> None:
        with self.db.tx():
            self.db.execute(
                "UPDATE control_state SET mode = 'PAUSED', reason = ?, set_by = ?, set_at = ? WHERE id = 1",
                (reason, by, self.clock.iso()),
            )
            self.db.audit("control.pause", {"reason": reason, "by": by})
        self.refresh()

    def resume(self, by: str = "operator") -> list[str]:
        """Clear the operator pause and every condition that needs a
        manual resume. Self-clearing conditions stay until their cause
        is gone. Returns the keys that were cleared."""
        with self.db.tx():
            cleared = [r["key"] for r in self.db.all("SELECT key FROM conditions WHERE auto_resume = 0")]
            self.db.execute("DELETE FROM conditions WHERE auto_resume = 0")
            self.db.execute(
                "UPDATE control_state SET mode = 'RUNNING', reason = NULL, set_by = ?, set_at = ? WHERE id = 1",
                (by, self.clock.iso()),
            )
            self.db.audit("control.resume", {"by": by, "cleared": cleared})
        self.refresh()
        return cleared

    def acknowledge(self, by: str = "operator") -> None:
        """Stop the repeating HALTED alert without resuming."""
        self.db.kv_set("halt_acknowledged_at", self.clock.iso())
        self.db.audit("control.ack", {"by": by})

    def halt_unacknowledged(self) -> bool:
        halts = self.db.all("SELECT set_at FROM conditions WHERE level = 'halt'")
        if not halts:
            return False
        acked = self.db.kv_get("halt_acknowledged_at")
        return acked is None or any(h["set_at"] > acked for h in halts)

    def operator_paused(self) -> bool:
        return self.control()["mode"] != "RUNNING"

    # ------------------------------------------------------------ conditions
    def set_condition(
        self,
        key: str,
        reason: str,
        *,
        level: str = BLOCK,
        scope: str = GLOBAL,
        auto_resume: bool = True,
        alert: Optional[bool] = None,
    ) -> bool:
        """Raise a condition. Returns True when it is new. A pause- or
        halt-level condition alerts the operator unless ``alert=False``."""
        assert level in _RANK
        existing = self.db.one("SELECT level, reason FROM conditions WHERE key = ?", (key,))
        if existing and existing["level"] == level:
            if existing["reason"] != reason:
                self.db.execute("UPDATE conditions SET reason = ? WHERE key = ?", (reason, key))
            return False
        with self.db.tx():
            self.db.execute(
                "INSERT INTO conditions (key, scope, level, reason, auto_resume, set_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET scope = excluded.scope, level = excluded.level, "
                "reason = excluded.reason, auto_resume = excluded.auto_resume, set_at = excluded.set_at",
                (key, scope, level, reason, 1 if auto_resume else 0, self.clock.iso()),
            )
            self.db.audit(
                "health.condition_set",
                {"key": key, "scope": scope, "level": level, "reason": reason, "auto_resume": auto_resume},
            )
        logger.warning("condition set: %s (%s, %s): %s", key, scope, level, reason)
        should_alert = alert if alert is not None else level in (PAUSE, HALT)
        if should_alert and self.alert:
            title = {HALT: "HALTED", PAUSE: "PAUSED", BLOCK: "Entries blocked"}[level]
            where = "" if scope == GLOBAL else f" [{scope}]"
            resume = "clears automatically" if auto_resume else "needs: botctl resume"
            self.alert(f"{title}{where}", f"{reason}\n({key}; {resume})", critical=level != BLOCK)
        self.refresh()
        return True

    def clear_condition(self, key: str) -> bool:
        if not self.db.one("SELECT 1 FROM conditions WHERE key = ?", (key,)):
            return False
        with self.db.tx():
            self.db.execute("DELETE FROM conditions WHERE key = ?", (key,))
            self.db.audit("health.condition_cleared", {"key": key})
        logger.info("condition cleared: %s", key)
        self.refresh()
        return True

    def clear_auto_conditions(self) -> list[str]:
        """Drop every self-clearing condition. Called at startup: each of
        them is re-derived from current facts by the first reconciliation
        pass, so one that no longer applies (for example about a market
        that is no longer configured) cannot linger. Conditions that need
        a manual resume are kept."""
        keys = [r["key"] for r in self.db.all("SELECT key FROM conditions WHERE auto_resume = 1")]
        if keys:
            with self.db.tx():
                self.db.execute("DELETE FROM conditions WHERE auto_resume = 1")
                self.db.audit("health.auto_conditions_reset", {"keys": keys})
            self.refresh()
        return keys

    def has_condition(self, key: str) -> bool:
        return self.db.one("SELECT 1 FROM conditions WHERE key = ?", (key,)) is not None

    def conditions(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM conditions ORDER BY set_at")

    # ------------------------------------------------------------ decisions
    def can_enter(self, coin: str) -> tuple[bool, Optional[str]]:
        """May a new entry be sent on ``coin`` right now?"""
        control = self.control()
        if control["mode"] != "RUNNING":
            return False, f"PAUSED: {control['reason'] or 'operator'}"
        row = self.db.one(
            "SELECT key, reason FROM conditions WHERE scope IN (?, ?) ORDER BY "
            "CASE level WHEN 'halt' THEN 0 WHEN 'pause' THEN 1 ELSE 2 END, set_at LIMIT 1",
            (GLOBAL, coin),
        )
        if row:
            return False, f"{row['key']}: {row['reason']}"
        return True, None

    def state(self) -> str:
        rows = self.db.all("SELECT scope, level FROM conditions")
        if any(r["level"] == HALT for r in rows):
            return "HALTED"
        if self.operator_paused() or any(r["level"] == PAUSE and r["scope"] == GLOBAL for r in rows):
            return "PAUSED"
        if rows:
            return "DEGRADED"
        return "HEALTHY"

    def refresh(self) -> str:
        """Record a state transition as an audit event (FR-071)."""
        state = self.state()
        if state != self._last_state:
            if self._last_state is not None:
                self.db.audit("health.state", {"from": self._last_state, "to": state})
                logger.warning("health %s -> %s", self._last_state, state)
            self._last_state = state
        return state

    def summary(self) -> dict[str, Any]:
        control = self.control()
        return {
            "state": self.state(),
            "control": {"mode": control["mode"], "reason": control["reason"], "set_by": control["set_by"], "set_at": control["set_at"]},
            "conditions": self.conditions(),
        }
