"""Read models shared by the local API, the dashboard and ``botctl``.

Everything here reads the database only, so the CLI can answer the
read-only verbs even when the bot process is down.
"""

from __future__ import annotations

from typing import Any, Optional

from hlbot.core.health import Health
from hlbot.db.database import Database, loads
from hlbot.domain.planning import parse_iso
from hlbot.domain.states import ORDER_TERMINAL, TRADE_TERMINAL
from hlbot.timeutil import Clock


def _age_s(clock: Clock, iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    return round((clock.now() - parse_iso(iso)).total_seconds(), 1)


def waiting_signals(db: Database) -> list[dict[str, Any]]:
    return db.all(
        "SELECT s.id, s.channel_ref, s.message_id, s.content_id, s.symbol, s.side, s.zone_low, s.zone_high, s.sl, s.tps, "
        "s.expires_at, s.attempts, s.reason, p.coin, p.effective_low, p.effective_high "
        "FROM signals s JOIN plans p ON p.signal_id = s.id WHERE s.state = 'WAITING_PRICE' ORDER BY s.id"
    )


def recent_signals(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    return db.all(
        "SELECT id, channel_ref, message_id, content_id, symbol, side, zone_low, zone_high, sl, state, reason, "
        "valid, validation_errors, signal_time, expires_at, attempts FROM signals WHERE type = 'signal' ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def open_trades(db: Database) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in TRADE_TERMINAL)
    return db.all(f"SELECT * FROM trades WHERE state NOT IN ({marks}) ORDER BY id", tuple(TRADE_TERMINAL))


def recent_trades(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    """The latest trades, each with its own orders (entry, stop-loss,
    every take-profit leg, close) in the order a trade lives through
    them, so the history can show what was placed and what filled."""
    return attach_orders(db, db.all("SELECT * FROM trades WHERE state != 'ABORTED' ORDER BY id DESC LIMIT ?", (limit,)))


def attach_orders(db: Database, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each trade its ``orders`` list, with one query for all of them."""
    if not trades:
        return trades
    by_trade: dict[int, list[dict[str, Any]]] = {t["id"]: [] for t in trades}
    ids = list(by_trade)
    for start in range(0, len(ids), 500):  # stay under SQLite's limit on bound parameters
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        orders = db.all(
            "SELECT id, trade_id, role, leg, attempt, side, type, tif, reduce_only, price, trigger_price, size, filled_size, "
            "avg_fill_price, state, exchange_status, error_text, created_at, updated_at "
            f"FROM orders WHERE trade_id IN ({marks}) "
            "ORDER BY trade_id, CASE role WHEN 'ENTRY' THEN 0 WHEN 'SL' THEN 1 WHEN 'TP' THEN 2 ELSE 3 END, leg, id",
            tuple(chunk),
        )
        for order in orders:
            by_trade[order.pop("trade_id")].append(order)
    for trade in trades:
        trade["orders"] = by_trade[trade["id"]]
    return trades


def live_orders(db: Database) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in ORDER_TERMINAL)
    return db.all(f"SELECT * FROM orders WHERE state NOT IN ({marks}) ORDER BY id", tuple(ORDER_TERMINAL))


def recent_orders(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))


def alerts(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT id, kind, critical, text, created_at, sent_at, dropped_at, attempts, last_error FROM outbox "
        "WHERE kind = 'alert' ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return rows


def latest_positions(db: Database, clock: Clock) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT p.* FROM position_snapshots p JOIN (SELECT coin, MAX(id) AS id FROM position_snapshots GROUP BY coin) x ON x.id = p.id"
    )
    for row in rows:
        row["age_s"] = _age_s(clock, row["at"])
    return rows


def latest_account(db: Database, clock: Clock) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT a.* FROM account_snapshots a JOIN (SELECT dex, MAX(id) AS id FROM account_snapshots GROUP BY dex) x ON x.id = a.id"
    )
    for row in rows:
        row["age_s"] = _age_s(clock, row["snapshot_at"])
    return rows


def day(db: Database, clock: Clock) -> dict[str, Any]:
    from hlbot.core.daystate import DayState

    state = DayState(db, clock)
    row = db.one("SELECT * FROM day_state WHERE utc_date = ?", (clock.utc_date(),)) or {}
    return {**row, **state.realized(), "utc_date": clock.utc_date()}


EQUITY_RANGES = {"1h": 3600, "1d": 86400, "1w": 7 * 86400}
WIN_OUTCOMES = ("WIN_ALL", "WIN_PARTIAL")


def trade_stats(db: Database, limit: int = 14, clock: Optional[Clock] = None) -> dict[str, Any]:
    """Closed-trade summary for the dashboard: gains and losses by net
    P/L (what the account actually made or lost, fees and funding
    included), the most recent closed trades for the P/L chart, and the
    win rate of the trades closed today (UTC, the same day as the day
    P/L). A trade that closed at exactly zero counts on neither side."""
    rows = db.all(
        "SELECT id, coin, side, outcome, net_pnl, closed_at FROM trades WHERE state = 'CLOSED' ORDER BY id DESC"
    )
    gains = [r["net_pnl"] for r in rows if (r["net_pnl"] or 0.0) > 0]
    losses = [r["net_pnl"] for r in rows if (r["net_pnl"] or 0.0) < 0]
    decided = len(gains) + len(losses)
    day = (clock or Clock()).utc_date()
    today = [r["net_pnl"] or 0.0 for r in rows if str(r["closed_at"] or "").startswith(day)]
    wins, lost = sum(1 for p in today if p > 0), sum(1 for p in today if p < 0)
    return {
        "closed": len(rows),
        "gain_count": len(gains),
        "gain_total": round(sum(gains), 6),
        "loss_count": len(losses),
        "loss_total": round(sum(losses), 6),
        "net_total": round(sum(r["net_pnl"] or 0.0 for r in rows), 6),
        "win_rate": round(len(gains) / decided, 4) if decided else None,
        "today": {
            "date": day,
            "closed": len(today),
            "wins": wins,
            "losses": lost,
            "win_rate": round(wins / (wins + lost), 4) if wins + lost else None,
        },
        "by_outcome": {o: sum(1 for r in rows if r["outcome"] == o) for o in sorted({r["outcome"] for r in rows if r["outcome"]})},
        "recent_closed": list(reversed(rows[:limit])),  # oldest first, as a chart reads
    }


def equity_history(
    db: Database, clock: Optional[Clock] = None, span: str = "1d", dexes: Optional[list[str]] = None, max_points: int = 240
) -> dict[str, Any]:
    """Account value over time from the stored snapshots, for the
    dashboard chart. ``dexes`` limits it to the perp dexes that are
    managed now, so a market that used to be configured does not bend
    the line. Thinned to at most ``max_points``, always keeping the
    latest point. ``from`` / ``to`` are the bounds of the requested window,
    so the chart's time axis shows the range that was asked for even when
    the stored history covers only part of it."""
    from datetime import timedelta

    clock = clock or Clock()
    seconds = EQUITY_RANGES.get(span)
    if seconds is None:
        raise ValueError(f"range must be one of {', '.join(EQUITY_RANGES)}")
    now = clock.now()
    cutoff = (now - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    window = {"range": span, "from": cutoff, "to": now.isoformat(timespec="milliseconds")}
    sql = "SELECT snapshot_at AS at, SUM(account_value) AS value FROM account_snapshots WHERE snapshot_at >= ?"
    params: list[Any] = [cutoff]
    if dexes is not None:
        if not dexes:
            return {**window, "points": []}
        sql += f" AND dex IN ({', '.join('?' for _ in dexes)})"
        params += list(dexes)
    rows = db.all(sql + " GROUP BY snapshot_at ORDER BY snapshot_at", tuple(params))
    if len(rows) > max_points:
        step = len(rows) / max_points
        picked = [rows[int(i * step)] for i in range(max_points - 1)] + [rows[-1]]
    else:
        picked = rows
    return {**window, "points": [{"at": r["at"], "value": r["value"]} for r in picked]}


def build_status(db: Database, clock: Optional[Clock] = None) -> dict[str, Any]:
    clock = clock or Clock()
    health = Health(db, clock).summary()
    return {
        "generated_at": clock.iso(),
        "health": health,
        "account": latest_account(db, clock),
        "positions": latest_positions(db, clock),
        "open_trades": open_trades(db),
        "live_orders": live_orders(db),
        "waiting_signals": waiting_signals(db),
        "recent_signals": recent_signals(db, 10),
        "recent_trades": recent_trades(db, 10),
        "alerts": alerts(db, 15),
        "day": day(db, clock),
        "stats": trade_stats(db, clock=clock),
        "outbox_pending": db.scalar("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND dropped_at IS NULL", default=0),
    }


def trace(db: Database, *, fill: Optional[int] = None, order: Optional[int] = None, trade: Optional[int] = None,
          signal: Optional[int] = None) -> dict[str, Any]:
    """Everything known about one fill, order, trade or signal, with its
    audit events in order (UC-09)."""
    if fill is not None:
        row = db.one("SELECT * FROM fill_trace WHERE fill_tid = ?", (fill,))
        if row is None:
            return {"found": False}
        order = order or row["order_id"]
        trade = trade or row["trade_id"]
        signal = signal or row["signal_id"]
    if order is not None and trade is None:
        row_o = db.get("orders", order)
        trade = row_o["trade_id"] if row_o else None
    if trade is not None and signal is None:
        row_t = db.get("trades", trade)
        signal = row_t["signal_id"] if row_t else None
    result: dict[str, Any] = {"found": any(v is not None for v in (order, trade, signal))}
    if signal is not None:
        sig = db.get("signals", signal)
        result["signal"] = sig
        if sig:
            result["message"] = db.one(
                "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?", (sig["channel_id"], sig["message_id"])
            )
            result["plan"] = db.one("SELECT * FROM plans WHERE signal_id = ?", (signal,))
            result["risk_assessments"] = db.all("SELECT * FROM risk_assessments WHERE signal_id = ? ORDER BY id", (signal,))
            result["trades"] = db.all("SELECT * FROM trades WHERE signal_id = ? ORDER BY id", (signal,))
    if trade is not None:
        result["trade"] = db.get("trades", trade)
        result["orders"] = db.all("SELECT * FROM orders WHERE trade_id = ? ORDER BY id", (trade,))
        result["fills"] = db.all("SELECT * FROM fills WHERE trade_id = ? ORDER BY time, tid", (trade,))
    clauses, params = [], []
    for column, value in (("signal_id", signal), ("trade_id", trade), ("order_id", order)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if clauses:
        events = db.all(f"SELECT * FROM audit_events WHERE {' OR '.join(clauses)} ORDER BY seq", tuple(params))
        for event in events:
            event["payload"] = loads(event["payload"], {})
        result["audit"] = events
    return result
