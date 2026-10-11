"""Trading analytics, the filtered trade history and the export data, all
from the records the bot itself keeps (trades, fills, account snapshots).

Rules, applied everywhere so the dashboard, the history list and the
exported workbook agree with each other and with the bot's own
accounting:

* One trade is one row, however many orders and fills it had. Its
  figures are the ones the bot recorded when it closed the trade from the
  exchange's fills: ``realized_pnl`` (sum of the fills' closed P/L),
  ``fees``, ``funding`` and ``net_pnl = realized - fees + funding``.
  Partial closes and several executions are therefore counted once.
* Performance figures use closed trades only. A win is a trade with a
  net result above zero, a loss one below zero; a trade at exactly zero
  counts on neither side (as in the overview's win rate).
* A trade belongs to the moment it closed. A trade still open belongs to
  the moment it opened (it has no result yet and appears only in counts
  and in the history).
* Times are stored in UTC. Date filters and the day / week / month
  buckets use the viewer's UTC offset, passed as ``tz`` in minutes.
* Nothing is estimated silently: a figure that cannot be computed from
  the records is ``None``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Optional

from hlbot.api.readmodel import attach_orders
from hlbot.db.database import Database
from hlbot.timeutil import Clock

STATUSES = ("all", "closed", "open")
SIDES = ("BUY", "SELL")
MAX_TZ_MINUTES = 14 * 60
PAGE_SIZES = (10, 20, 50, 100)
EQUITY_POINTS = 240


@dataclass(frozen=True)
class TradeFilter:
    date_from: Optional[str] = None  # the viewer's local dates, both inclusive
    date_to: Optional[str] = None
    tz_minutes: int = 0  # east of UTC: +420 for UTC+07:00
    symbols: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    side: Optional[str] = None
    status: str = "all"

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(minutes=self.tz_minutes))

    @property
    def tz_label(self) -> str:
        sign, minutes = ("+" if self.tz_minutes >= 0 else "-"), abs(self.tz_minutes)
        return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"

    def bounds_utc(self) -> tuple[Optional[str], Optional[str]]:
        """The date range as UTC instants: from 00:00 local on the first day up to, not including, 00:00 after the last."""
        def instant(day: str, add_days: int) -> str:
            local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=self.tz) + timedelta(days=add_days)
            return local.astimezone(UTC).isoformat(timespec="milliseconds")

        return (instant(self.date_from, 0) if self.date_from else None, instant(self.date_to, 1) if self.date_to else None)

    def describe(self) -> dict[str, Any]:
        low, high = self.bounds_utc()
        return {"from": self.date_from, "to": self.date_to, "tz_minutes": self.tz_minutes, "tz": self.tz_label, "from_utc": low, "to_utc": high,
                "symbols": list(self.symbols), "sources": list(self.sources), "side": self.side, "status": self.status}


def _list(value: Optional[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in (value or "").split(",") if part.strip()))


def parse_filter(query: Mapping[str, str]) -> TradeFilter:
    """Validate the filter a client sent. Raises ``ValueError`` with a message fit to show."""
    def day(key: str) -> Optional[str]:
        raw = (query.get(key) or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{key} must be a date like 2026-10-11") from exc

    date_from, date_to = day("from"), day("to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("the start date is after the end date")
    try:
        tz_minutes = int((query.get("tz") or "0").strip())
    except ValueError as exc:
        raise ValueError("tz must be a whole number of minutes") from exc
    if abs(tz_minutes) > MAX_TZ_MINUTES:
        raise ValueError("tz is out of range")
    side = (query.get("side") or "").strip().upper() or None
    if side is not None and side not in SIDES:
        raise ValueError("side must be BUY or SELL")
    status = (query.get("status") or "all").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    symbols, sources = _list(query.get("symbol")), _list(query.get("source"))
    if len(symbols) > 50 or len(sources) > 50 or any(len(v) > 80 for v in symbols + sources):
        raise ValueError("too many or too long symbol / source values")
    return TradeFilter(date_from, date_to, tz_minutes, symbols, sources, side, status)


# ------------------------------------------------------------------ selecting trades
_TRADE_AT = "COALESCE(t.closed_at, t.opened_at, t.created_at)"


def _where(f: TradeFilter) -> tuple[str, list[Any]]:
    clauses, params = ["t.state != 'ABORTED'"], []  # an aborted entry never held a position
    low, high = f.bounds_utc()
    if low:
        clauses.append(f"{_TRADE_AT} >= ?")
        params.append(low)
    if high:
        clauses.append(f"{_TRADE_AT} < ?")
        params.append(high)
    if f.symbols:
        clauses.append(f"t.coin IN ({', '.join('?' for _ in f.symbols)})")
        params += list(f.symbols)
    if f.sources:
        clauses.append(f"COALESCE(s.channel_ref, '') IN ({', '.join('?' for _ in f.sources)})")
        params += list(f.sources)
    if f.side:
        clauses.append("t.side = ?")
        params.append(f.side)
    if f.status == "closed":
        clauses.append("t.state = 'CLOSED'")
    elif f.status == "open":
        clauses.append("t.state != 'CLOSED'")
    return " AND ".join(clauses), params


_SELECT = f"SELECT t.*, COALESCE(s.channel_ref, '') AS source, s.content_id AS signal_content, {_TRADE_AT} AS at FROM trades t LEFT JOIN signals s ON s.id = t.signal_id"


def select_trades(db: Database, f: TradeFilter, *, limit: Optional[int] = None, offset: int = 0) -> list[dict[str, Any]]:
    where, params = _where(f)
    sql = f"{_SELECT} WHERE {where} ORDER BY {_TRADE_AT} DESC, t.id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return db.all(sql, tuple(params))


def count_trades(db: Database, f: TradeFilter) -> int:
    where, params = _where(f)
    return int(db.scalar(f"SELECT COUNT(*) FROM trades t LEFT JOIN signals s ON s.id = t.signal_id WHERE {where}", tuple(params), default=0))


def filter_options(db: Database) -> dict[str, list[str]]:
    """What the filter drop-downs can offer: only values that occur in the history."""
    symbols = [r["coin"] for r in db.all("SELECT DISTINCT coin FROM trades WHERE state != 'ABORTED' ORDER BY coin")]
    sources = [r["source"] for r in db.all(
        "SELECT DISTINCT COALESCE(s.channel_ref, '') AS source FROM trades t LEFT JOIN signals s ON s.id = t.signal_id WHERE t.state != 'ABORTED' ORDER BY 1")]
    return {"symbols": symbols, "sources": [s for s in sources if s], "sides": list(SIDES), "statuses": list(STATUSES)}


def _fill_facts(db: Database, trades: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """Per trade, from its fills: traded value on each side and the size closed. One row per fill, so nothing is counted twice."""
    facts: dict[int, dict[str, float]] = {}
    ids = [t["id"] for t in trades]
    sides = {t["id"]: t["side"] for t in trades}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = db.all(f"SELECT trade_id, side, price, size FROM fills WHERE trade_id IN ({', '.join('?' for _ in chunk)})", tuple(chunk))
        for row in rows:
            fact = facts.setdefault(row["trade_id"], {"volume": 0.0, "exit_value": 0.0, "exit_size": 0.0, "fills": 0})
            value = float(row["price"]) * float(row["size"])
            fact["volume"] += value
            fact["fills"] += 1
            if row["side"] != sides[row["trade_id"]]:  # a fill against the trade's direction closes part of it
                fact["exit_value"] += value
                fact["exit_size"] += float(row["size"])
    return facts


def planned_rr(trade: Mapping[str, Any]) -> Optional[float]:
    """Reward to risk as the signal planned it: the size-weighted distance to the take-profits over the distance to the stop."""
    entry, stop = trade.get("avg_entry"), trade.get("sl_price")
    try:
        levels = json.loads(trade.get("tp_plan") or "[]")
    except (TypeError, ValueError):
        return None
    if not entry or not stop or not isinstance(levels, list) or abs(entry - stop) <= 0:
        return None
    size = sum(float(level.get("size") or 0) for level in levels)
    if size <= 0:
        return None
    reward = sum(float(level["size"]) * abs(float(level["price"]) - entry) for level in levels) / size
    return round(reward / abs(entry - stop), 4)


def enrich(db: Database, trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add what the history and the export show beside the stored columns: exit price, traded value, duration, planned R:R."""
    facts = _fill_facts(db, trades)
    for trade in trades:
        fact = facts.get(trade["id"], {})
        trade["exit_price"] = round(fact["exit_value"] / fact["exit_size"], 8) if fact.get("exit_size") else None
        trade["volume_quote"] = round(fact.get("volume", 0.0), 6)
        trade["fill_count"] = int(fact.get("fills", 0))
        trade["planned_rr"] = planned_rr(trade)
        trade["duration_s"] = None
        if trade.get("opened_at") and trade.get("closed_at"):
            trade["duration_s"] = round((datetime.fromisoformat(trade["closed_at"]) - datetime.fromisoformat(trade["opened_at"])).total_seconds(), 3)
    return trades


def trades_page(db: Database, f: TradeFilter, page: int, page_size: int) -> dict[str, Any]:
    total = count_trades(db, f)
    pages = max(1, math.ceil(total / page_size))
    page = min(max(1, page), pages)
    trades = attach_orders(db, enrich(db, select_trades(db, f, limit=page_size, offset=(page - 1) * page_size)))
    return {"filter": f.describe(), "options": filter_options(db), "total": total, "page": page, "pages": pages, "page_size": page_size, "trades": trades}


# ------------------------------------------------------------------ analytics
def _local(iso: str, f: TradeFilter) -> datetime:
    return datetime.fromisoformat(iso).astimezone(f.tz)


def _group(rows: list[dict[str, Any]], key: Any) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        g = groups.setdefault(key(row), {"key": key(row), "trades": 0, "wins": 0, "losses": 0, "net": 0.0, "fees": 0.0, "volume": 0.0})
        net = float(row["net_pnl"] or 0.0)
        g["trades"] += 1
        g["wins"] += net > 0
        g["losses"] += net < 0
        g["net"] += net
        g["fees"] += float(row["fees"] or 0.0)
        g["volume"] += float(row.get("volume_quote") or 0.0)
    for g in groups.values():
        decided = g["wins"] + g["losses"]
        g["win_rate"] = round(g["wins"] / decided, 4) if decided else None
        g["net"], g["fees"], g["volume"] = round(g["net"], 6), round(g["fees"], 6), round(g["volume"], 2)
    return list(groups.values())


def _nice_step(span: float, target: int) -> float:
    if span <= 0:
        return 1.0
    raw = span / target
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)


def distribution(values: list[float], target_bins: int = 10) -> list[dict[str, Any]]:
    """Histogram of per-trade results. Zero is always a bin edge, so losses and wins never share a bar."""
    if not values:
        return []
    low, high = min(values + [0.0]), max(values + [0.0])
    step = _nice_step(high - low, target_bins) if high > low else max(abs(high), 1.0)
    first = math.floor(low / step) * step
    count = max(1, math.ceil((high - first) / step + 1e-9))
    bins = [{"from": round(first + i * step, 10), "to": round(first + (i + 1) * step, 10), "count": 0} for i in range(count)]
    for value in values:
        index = min(count - 1, max(0, math.floor((value - first) / step + 1e-12)))  # a result of exactly zero falls in the first bin from zero up
        bins[index]["count"] += 1
    return bins


def max_drawdown(cumulative: list[float]) -> tuple[float, Optional[int], Optional[int]]:
    """Largest fall from a running peak of the cumulative result (the peak starts at zero). Returns (amount, peak index, trough index)."""
    peak, worst = 0.0, 0.0
    peak_at: Optional[int] = None  # None while the peak is still the starting zero
    from_at: Optional[int] = None
    to_at: Optional[int] = None
    for i, value in enumerate(cumulative):
        if value > peak:
            peak, peak_at = value, i
        if peak - value > worst:
            worst, from_at, to_at = peak - value, peak_at, i
    return round(worst, 6), from_at, to_at


def equity_series(db: Database, f: TradeFilter, dexes: Optional[list[str]]) -> list[dict[str, Any]]:
    """Account value over time from the stored snapshots (summed over the managed dexes). Account-wide: only the date range applies."""
    low, high = f.bounds_utc()
    sql, params = "SELECT snapshot_at AS at, SUM(account_value) AS value FROM account_snapshots WHERE 1 = 1", []
    if low:
        sql += " AND snapshot_at >= ?"
        params.append(low)
    if high:
        sql += " AND snapshot_at < ?"
        params.append(high)
    if dexes is not None:
        if not dexes:
            return []
        sql += f" AND dex IN ({', '.join('?' for _ in dexes)})"
        params += list(dexes)
    rows = db.all(sql + " GROUP BY snapshot_at ORDER BY snapshot_at", tuple(params))
    if len(rows) > EQUITY_POINTS:
        step = len(rows) / EQUITY_POINTS
        rows = [rows[int(i * step)] for i in range(EQUITY_POINTS - 1)] + [rows[-1]]
    return [{"at": r["at"], "value": round(float(r["value"] or 0.0), 6)} for r in rows]


def analytics(db: Database, f: TradeFilter, clock: Optional[Clock] = None, dexes: Optional[list[str]] = None) -> dict[str, Any]:
    clock = clock or Clock()
    matched = enrich(db, select_trades(db, f))
    closed = sorted((t for t in matched if t["state"] == "CLOSED"), key=lambda t: (t["closed_at"] or t["at"], t["id"]))
    nets = [float(t["net_pnl"] or 0.0) for t in closed]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n < 0]
    gross_profit, gross_loss = sum(wins), sum(losses)
    rrs = [t["planned_rr"] for t in closed if t["planned_rr"] is not None]

    running, cumulative = 0.0, []
    for trade, net in zip(closed, nets, strict=True):
        running += net
        cumulative.append({"at": trade["closed_at"] or trade["at"], "value": round(running, 6), "trade_id": trade["id"], "net": round(net, 6)})
    drawdown, peak_at, trough_at = max_drawdown([p["value"] for p in cumulative])

    equity = equity_series(db, f, dexes)
    # Realised balance, derived: the first account value in the range plus every closed trade's net result since then
    # (all symbols: an account has one balance). It ignores deposits, withdrawals and open positions; the equity line does not.
    balance: list[dict[str, Any]] = []
    if equity:
        account_wide = TradeFilter(f.date_from, f.date_to, f.tz_minutes, status="closed")
        level = equity[0]["value"]
        balance.append({"at": equity[0]["at"], "value": level})
        for trade in sorted(select_trades(db, account_wide), key=lambda t: (t["closed_at"] or "", t["id"])):
            if (trade["closed_at"] or "") >= equity[0]["at"]:
                level += float(trade["net_pnl"] or 0.0)
                balance.append({"at": trade["closed_at"], "value": round(level, 6)})
        if balance[-1]["at"] < equity[-1]["at"]:  # nothing closed since: the balance stays level up to the latest snapshot
            balance.append({"at": equity[-1]["at"], "value": round(level, 6)})
    reference = equity[0]["value"] if equity else None

    def period(kind: str) -> list[dict[str, Any]]:
        def key(trade: dict[str, Any]) -> str:
            local = _local(trade["closed_at"] or trade["at"], f)
            if kind == "month":
                return local.strftime("%Y-%m")
            if kind == "week":
                return (local - timedelta(days=local.weekday())).strftime("%Y-%m-%d")  # the Monday the week starts on
            return local.strftime("%Y-%m-%d")

        return sorted(_group(closed, key), key=lambda g: g["key"])

    latest = db.one("SELECT snapshot_at FROM account_snapshots ORDER BY snapshot_at DESC LIMIT 1")
    account = None
    if latest:
        sql, params = "SELECT SUM(account_value) AS value, SUM(withdrawable) AS withdrawable, SUM(margin_used) AS margin FROM account_snapshots WHERE snapshot_at = ?", [latest["snapshot_at"]]
        if dexes:
            sql += f" AND dex IN ({', '.join('?' for _ in dexes)})"
            params += list(dexes)
        row = db.one(sql, tuple(params)) or {}
        if row.get("value") is not None:
            account = {"at": latest["snapshot_at"], "equity": round(float(row["value"]), 6), "withdrawable": round(float(row["withdrawable"] or 0.0), 6),
                       "margin_used": round(float(row["margin"] or 0.0), 6)}

    decided = len(wins) + len(losses)
    avg_win = gross_profit / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    kpis = {
        "total_trades": len(closed),
        "open_trades": sum(1 for t in matched if t["state"] != "CLOSED"),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(nets) - decided,
        "win_rate": round(len(wins) / decided, 4) if decided else None,
        "net_pnl": round(sum(nets), 6),
        "gross_profit": round(gross_profit, 6),
        "gross_loss": round(gross_loss, 6),
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss < 0 else None,
        "avg_win": round(avg_win, 6) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 6) if avg_loss is not None else None,
        "payoff_ratio": round(avg_win / abs(avg_loss), 4) if avg_win is not None and avg_loss else None,
        "expectancy": round(sum(nets) / len(nets), 6) if nets else None,
        "avg_planned_rr": round(sum(rrs) / len(rrs), 4) if rrs else None,
        "max_drawdown": drawdown,
        "max_drawdown_pct": round(drawdown / (reference + (cumulative[peak_at]["value"] if peak_at is not None else 0.0)), 6) if reference and drawdown else None,
        "max_drawdown_from": cumulative[peak_at]["at"] if peak_at is not None else None,
        "max_drawdown_to": cumulative[trough_at]["at"] if trough_at is not None else None,
        "realized_before_costs": round(sum(float(t["realized_pnl"] or 0.0) for t in closed), 6),
        "fees": round(sum(float(t["fees"] or 0.0) for t in closed), 6),
        "funding": round(sum(float(t["funding"] or 0.0) for t in closed), 6),
        "volume_quote": round(sum(float(t["volume_quote"] or 0.0) for t in closed), 2),
        "account": account,
    }
    return {
        "generated_at": clock.iso(),
        "filter": f.describe(),
        "options": filter_options(db),
        "matched_trades": len(matched),
        "kpis": kpis,
        "cumulative": cumulative,
        # where the line starts: at zero, when the first of these trades was opened
        "cumulative_start": {"at": closed[0]["opened_at"] or closed[0]["at"], "value": 0.0} if closed else None,
        "equity": equity,
        "balance": balance,
        "periods": {"day": period("day"), "week": period("week"), "month": period("month")},
        "distribution": distribution(nets),
        "outcomes": sorted(({"key": o or "–", "trades": n} for o, n in _count(t["outcome"] for t in closed).items()), key=lambda r: -r["trades"]),
        "by_symbol": sorted(_group(closed, lambda t: t["coin"]), key=lambda g: g["key"]),
        "by_source": sorted(_group(closed, lambda t: t["source"] or "unknown"), key=lambda g: g["key"]),
        "by_side": sorted(_group(closed, lambda t: t["side"]), key=lambda g: g["key"]),
    }


def _count(values: Any) -> dict[Any, int]:
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ------------------------------------------------------------------ export data
def export_data(db: Database, f: TradeFilter) -> dict[str, Any]:
    """Everything the workbook holds for a filter: every matching trade (not one page of them), their executions and the summary figures."""
    trades = enrich(db, select_trades(db, f))
    entry_oids: dict[int, Any] = {}
    fills: list[dict[str, Any]] = []
    ids = [t["id"] for t in trades]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        for row in db.all(f"SELECT trade_id, oid FROM orders WHERE role = 'ENTRY' AND trade_id IN ({marks}) ORDER BY id", tuple(chunk)):
            entry_oids.setdefault(row["trade_id"], row["oid"])
        fills += db.all(
            "SELECT f.tid, f.trade_id, f.oid, COALESCE(f.role, o.role) AS role, f.coin, f.side, f.price, f.size, f.fee, f.fee_token, f.closed_pnl, f.time, f.liquidation "
            f"FROM fills f LEFT JOIN orders o ON o.id = f.order_id WHERE f.trade_id IN ({marks})", tuple(chunk))
    for trade in trades:
        trade["entry_oid"] = entry_oids.get(trade["id"])
    fills.sort(key=lambda r: (r["time"], r["tid"]))
    return {"filter": f.describe(), "trades": trades, "fills": fills, "kpis": analytics(db, f)["kpis"]}
