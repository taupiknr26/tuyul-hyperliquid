"""The trade-history workbook: what goes into which sheet.

Three sheets: ``Trades`` (one row per trade), ``Executions`` (one row per
fill, so a trade's partial closes can be followed) and ``Summary`` (the
filter that was used and the key figures for exactly these trades).

Only trading records are written. No account address, key, token or
client order id leaves the bot this way.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Optional

from hlbot.api.xlsx import Sheet, excel_datetime, workbook

MAX_TP_COLUMNS = 5
MAX_TRADES = 100_000


def _when(iso: Optional[str], tz: timezone) -> Optional[float]:
    if not iso:
        return None
    try:
        return excel_datetime(datetime.fromisoformat(iso), tz)
    except ValueError:
        return None


def _when_ms(ms: Any, tz: timezone) -> Optional[float]:
    """Fill times are stored as milliseconds since the epoch."""
    try:
        return excel_datetime(datetime.fromtimestamp(int(ms) / 1000, tz=UTC), tz)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _levels(trade: dict[str, Any]) -> list[Optional[float]]:
    try:
        plan = json.loads(trade.get("tp_plan") or "[]")
        prices = [float(level["price"]) for level in sorted(plan, key=lambda level: level.get("level", 0))]
    except (TypeError, ValueError, KeyError):
        prices = []
    return (prices + [None] * MAX_TP_COLUMNS)[:MAX_TP_COLUMNS]


def _zone(data: dict[str, Any]) -> timezone:
    return timezone(timedelta(minutes=int(data["filter"]["tz_minutes"])))


def filename(data: dict[str, Any], now: datetime) -> str:
    return f"trade-history-{now.astimezone(_zone(data)).strftime('%Y-%m-%d')}.xlsx"


def build_workbook(data: dict[str, Any], *, env_label: str, generated: datetime) -> bytes:
    f = data["filter"]
    tz = _zone(data)
    zone = f["tz"]
    trades, fills, k = data["trades"], data["fills"], data["kpis"]

    trade_columns = [
        ("Trade ID", "int", 9), ("Signal ID", "int", 10), ("Signal source", "text", 18), ("Signal", "text", 32), ("Symbol", "text", 10), ("Direction", "text", 10),
        ("Status", "text", 16), ("Outcome", "text", 14), ("Close reason", "text", 14), ("Planned size", "size", 13), ("Filled size", "size", 13),
        ("Entry price", "price", 13), ("Exit price", "price", 13), (f"Entry time ({zone})", "datetime", 20), (f"Exit time ({zone})", "datetime", 20), ("Duration", "duration", 11),
        ("Stop loss", "price", 12), *[(f"TP{i}", "price", 12) for i in range(1, MAX_TP_COLUMNS + 1)],
        ("Gross P&L", "money", 12), ("Fees", "money", 10), ("Funding", "money", 10), ("Net P&L", "money", 12), ("Planned R:R", "ratio", 12),
        ("Traded value", "number", 14), ("Executions", "int", 11), ("Entry order ID", "text", 16), ("Environment", "text", 12),
    ]

    def trade_rows() -> Any:
        for t in trades:
            yield [
                t["id"], t["signal_id"], t.get("source") or "", t.get("signal_content") or "", t["coin"], t["side"], t["state"], t.get("outcome") or "", t.get("close_reason") or "",
                t["planned_size"], t["filled_size"], t["avg_entry"], t["exit_price"], _when(t["opened_at"], tz), _when(t["closed_at"], tz),
                t["duration_s"] / 86400 if t["duration_s"] is not None else None, t["sl_price"], *_levels(t),
                t["realized_pnl"], t["fees"], t["funding"], t["net_pnl"], t["planned_rr"], t["volume_quote"] or None, t["fill_count"],
                str(t["entry_oid"]) if t.get("entry_oid") else "", t.get("env") or "",
            ]

    fill_columns = [
        ("Execution ID", "text", 20), ("Trade ID", "int", 9), ("Order ID", "text", 16), ("Order role", "text", 11), ("Symbol", "text", 10), ("Side", "text", 8),
        ("Price", "price", 13), ("Size", "size", 12), ("Value", "number", 13), ("Fee", "money", 10), ("Fee currency", "text", 12), ("Closed P&L", "money", 12),
        (f"Time ({zone})", "datetime", 20), ("Liquidation", "text", 11),
    ]

    def fill_rows() -> Any:
        for x in fills:
            yield [str(x["tid"]), x["trade_id"], str(x["oid"]) if x["oid"] else "", x["role"] or "", x["coin"], x["side"], x["price"], x["size"],
                   float(x["price"]) * float(x["size"]), x["fee"], x["fee_token"] or "", x["closed_pnl"], _when_ms(x["time"], tz), "yes" if x["liquidation"] else ""]

    def text(value: Any) -> tuple[Any, str]:
        return (value if value not in (None, "", []) else "all", "text")

    summary: list[list[Any]] = [
        ["Generated", (excel_datetime(generated, tz), "datetime"), zone],
        ["Environment", (env_label, "text"), ""],
        ["Period", text(f"{f['from'] or 'start'} to {f['to'] or 'today'}" if f["from"] or f["to"] else None), f"dates in {zone}; a trade belongs to the moment it closed"],
        ["Symbols", text(", ".join(f["symbols"])), ""],
        ["Signal sources", text(", ".join(f["sources"])), ""],
        ["Direction", text(f["side"]), ""],
        ["Status", text(f["status"]), ""],
        ["Trades in this file", (len(trades), "int"), "every trade matching the filter, not one page of them"],
        ["Executions in this file", (len(fills), "int"), ""],
        ["", None, ""],
        ["Closed trades", (k["total_trades"], "int"), "the figures below use closed trades only"],
        ["Winning trades", (k["wins"], "int"), "net result above zero"],
        ["Losing trades", (k["losses"], "int"), "net result below zero"],
        ["Win rate", (k["win_rate"], "percent"), "wins / (wins + losses)"],
        ["Net P&L", (k["net_pnl"], "money"), "realised - fees + funding"],
        ["Gross profit", (k["gross_profit"], "money"), "sum of the winning trades' net results"],
        ["Gross loss", (k["gross_loss"], "money"), "sum of the losing trades' net results"],
        ["Profit factor", (k["profit_factor"], "ratio"), "gross profit / |gross loss|"],
        ["Average win", (k["avg_win"], "money"), ""],
        ["Average loss", (k["avg_loss"], "money"), ""],
        ["Average planned R:R", (k["avg_planned_rr"], "ratio"), "size-weighted distance to the take-profits / distance to the stop"],
        ["Maximum drawdown", (k["max_drawdown"], "money"), "largest fall of the cumulative net result from its peak"],
        ["Realised before costs", (k["realized_before_costs"], "money"), ""],
        ["Fees", (k["fees"], "money"), ""],
        ["Funding", (k["funding"], "money"), ""],
        ["Traded value", (k["volume_quote"], "number"), "sum of price x size over every execution"],
    ]
    return workbook(
        [
            Sheet("Trades", trade_columns, trade_rows()),
            Sheet("Executions", fill_columns, fill_rows()),
            Sheet("Summary", [("Metric", "text", 26), ("Value", "text", 22), ("Note", "text", 64)], summary, table=False),
        ],
        title="Trade history",
        created=generated,
    )
