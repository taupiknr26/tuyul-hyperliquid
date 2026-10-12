"""UTC-day P/L state for the daily loss limit and profit target
(FR-033, FR-034; fixes LIM-16).

Nothing here lives only in memory. The day's P/L is recomputed from the
fills and funding tables every time it is needed, so it survives a
restart and cannot drift:

    day P/L = realised (closed P/L) - fees + funding + unrealised

The starting equity for the percentage limits is the first account
value seen on that UTC day. Deposits and withdrawals do not affect the
P/L figure because it is built from fills, not from the balance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from hlbot.db.database import Database
from hlbot.exchange.base import AccountState
from hlbot.timeutil import Clock, day_start_ms


class DayState:
    def __init__(self, db: Database, clock: Optional[Clock] = None):
        self.db = db
        self.clock = clock or Clock()

    def ensure(self, equity: Optional[float]) -> Optional[dict[str, Any]]:
        """Row for today, created from the first *positive* equity seen.
        An unfunded account has no base for percentage limits: no row is
        created, and the callers fail closed on the missing figure."""
        today = self.clock.utc_date()
        row = self.db.one("SELECT * FROM day_state WHERE utc_date = ?", (today,))
        if row is not None and row["start_equity"] <= 0 and equity is not None and equity > 0:
            # The day was opened before the account was funded: re-base it,
            # and drop markers that were computed against zero.
            with self.db.tx():
                self.db.execute(
                    "UPDATE day_state SET start_equity = ?, loss_limit_hit_at = NULL, target_hit_at = NULL WHERE utc_date = ?",
                    (float(equity), today),
                )
                self.db.audit("day.start", {"utc_date": today, "start_equity": equity, "rebased": True})
            return self.db.one("SELECT * FROM day_state WHERE utc_date = ?", (today,))
        if row is None and equity is not None and equity > 0:
            with self.db.tx():
                self.db.execute(
                    "INSERT OR IGNORE INTO day_state (utc_date, start_equity) VALUES (?, ?)", (today, float(equity))
                )
                self.db.audit("day.start", {"utc_date": today, "start_equity": equity})
            row = self.db.one("SELECT * FROM day_state WHERE utc_date = ?", (today,))
        return row

    def realized(self, utc_date: Optional[str] = None) -> dict[str, float]:
        start = day_start_ms(utc_date or self.clock.utc_date())
        end = start + 86_400_000
        fills = self.db.one(
            "SELECT COALESCE(SUM(closed_pnl), 0) AS pnl, COALESCE(SUM(fee), 0) AS fees FROM fills WHERE time >= ? AND time < ?",
            (start, end),
        ) or {"pnl": 0.0, "fees": 0.0}
        funding = self.db.scalar(
            "SELECT COALESCE(SUM(usdc), 0) FROM funding_payments WHERE time >= ? AND time < ?", (start, end), default=0.0
        )
        return {"realized": float(fills["pnl"]), "fees": float(fills["fees"]), "funding": float(funding)}

    def pnl(self, account: Optional[AccountState], coins: Sequence[str]) -> Optional[float]:
        """Today's P/L, or None when the unrealised part cannot be read."""
        if account is None:
            return None
        parts = self.realized()
        unrealized = sum(p.unrealized_pnl for c, p in account.positions.items() if c in coins)
        return parts["realized"] - parts["fees"] + parts["funding"] + unrealized

    def mark(self, column: str) -> bool:
        """Set a once-per-day marker. Returns True the first time."""
        assert column in ("loss_limit_hit_at", "target_hit_at", "target_notified")
        today = self.clock.utc_date()
        row = self.db.one(f"SELECT {column} AS v FROM day_state WHERE utc_date = ?", (today,))
        if row is None or row["v"]:
            return False
        value: Any = 1 if column == "target_notified" else self.clock.iso()
        self.db.execute(f"UPDATE day_state SET {column} = ? WHERE utc_date = ?", (value, today))
        return True
