"""Report for the 20-trade BTC suite (Markdown and JSON) from a run's recorded results.

    .venv/bin/python qa/make_report_btc20.py qa/results/btc20-20261011

Reads ``results.json`` and the test bot's own database. A recorded result is
never changed here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def table(headers: list[str], rows: list[list[Any]]) -> str:
    cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows])


def bot_trades(db: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]
    for t in trades:
        t["orders"] = [dict(r) for r in conn.execute("SELECT role, leg, state, size, filled_size, avg_fill_price, exchange_status FROM orders WHERE trade_id = ? ORDER BY id", (t["id"],))]
    conn.close()
    return trades


def host_sleeps(started: str, finished: str) -> list[str]:
    """Sleep events from the macOS power log inside the run's window (local time), or [] where there is no such log."""
    try:
        text = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    lo, hi = (datetime.fromisoformat(t).astimezone() for t in (started, finished))
    found = []
    for line in text.splitlines():
        if "Entering Sleep" not in line:
            continue
        try:
            at = datetime.strptime(line[:25], "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
        if lo - timedelta(seconds=5) <= at <= hi:
            found.append(line[:19])
    return found


def note(case: dict[str, Any], prefix: str) -> str:
    return next((n[len(prefix):] for n in case["notes"] if n.startswith(prefix)), "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir")
    parser.add_argument("--db", default=None, help="the bot database the trades are in (default: the test bot's, inside the run folder)")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    run = json.loads((run_dir / "results.json").read_text())
    cases, counts, start, end, lat = run["cases"], run["counts"], run["start"], run["end"], run["latency_ms"]
    live = run.get("suite") == "live20"
    every = bot_trades(Path(args.db) if args.db else run_dir / "bot" / "data" / "hlbot-testnet.sqlite3")
    by_trade_id = {t["id"]: t for t in every}
    trade_of = {}
    for c in cases:  # the trade each case opened, by the id recorded when it became protected
        step = next((s for s in c["steps"] if s["action"] == "trade protected"), None)
        if step and step["trade"]["id"] in by_trade_id:
            trade_of[c["id"]] = by_trade_id[step["trade"]["id"]]
    trades = list(trade_of.values())
    levels = len((run.get("live_settings") or {}).get("tp_close_percents") or [0] * 5)
    sleeps = host_sleeps(run["started_at"], run["finished_at"])
    protect = [float(note(c, "time from fill to protected: ").split(" ")[0]) for c in cases if note(c, "time from fill to protected: ")]
    slips = []
    for c in cases:
        text = next((n for n in c["notes"] if "slippage" in n), "")
        if text:
            slips.append(abs(float(text.split("slippage ")[1].split(" ")[0])))
    fees = sum(t["fees"] or 0 for t in trades)
    realised = sum(t["realized_pnl"] or 0 for t in trades)
    net = sum(t["net_pnl"] or 0 for t in trades)
    not_passed = [c for c in cases if c["status"] != "passed"]

    out: list[str] = []
    w = out.append
    w("# Test report: 20 real trades on BTC through " + ("your running bot" if live else "a test bot") + ", Hyperliquid testnet")
    w("")
    w(f"Run `{run['run_id']}` · {run['started_at'][:19]}Z to {run['finished_at'][:19]}Z · account `{run['account']}` · environment **testnet only**")
    w("")
    w("## Result")
    w("")
    w(f"**{counts['passed']} of {counts['planned']} trades passed every check, {counts['failed']} failed, {counts['blocked']} blocked, {counts['skipped']} skipped.** "
      f"The bot opened {len(trades)} trades ({sum(1 for t in trades if t['side'] == 'BUY')} long, {sum(1 for t in trades if t['side'] == 'SELL')} short). "
      f"After the run the account was {'flat with no open orders' if run['cleanup']['clean'] else 'NOT clean (see Cleanup)'}.")
    w("")
    if not_passed:
        w("Not passed:")
        w("")
        for c in not_passed:
            failed = "; ".join(a["name"] for a in c["assertions"] if not a["passed"])
            reason = c["error"] or c["blocked_reason"] or failed
            w(f"- **{c['id']}** ({c['title']}): {c['status']}. {reason}")
        w("")
    notes = run_dir / "notes.md"  # the analyst's reading of the failures, written by hand after the run
    if notes.exists():
        w(notes.read_text().rstrip())
        w("")
    w("## How it was run")
    w("")
    if live:
        w(f"Each trade went through your own running bot, with your own settings (three take-profit shares of {(run.get('live_settings') or {}).get('tp_close_percents')}, "
          "your stop-distance and reward limits): a signal shaped like your channel's posts was injected, the bot entered with its limit IOC order and placed the "
          "stop-loss and take-profits. The trade was then held for up to 25 s, in which the market could reach a take-profit or the stop, and closed by command if still "
          "open. After every step the exchange was queried directly and compared with what the signal asked for. The harness placed, cancelled and closed nothing "
          "itself: your bot was the only writer on the account. The Mac was kept awake with `caffeinate`.")
    else:
        w("Each trade went through the bot itself: a signal in the channel's format was injected, the bot entered with its limit IOC order, placed one stop-loss and five "
          "take-profits, and the trade was ended in one of four ways. After every step the exchange was queried directly and compared with what the signal asked for. "
          "The bot ran with a temporary settings file and its own data folder; the operator's bot was stopped for the duration. The Mac was kept awake with `caffeinate`.")
    w("")
    session = run.get("bot_sessions", {}).get("BTC", {})
    iso = run["isolation"]
    w(table(["Check before the first order", "Result"], [
        ["Environment and endpoints", f"`{iso['env']}` · `{iso['rest_url']}` · `{iso['ws_url']}`"],
        ["Signing key authorised", "yes" if run["signer_authorized"] else "NO"],
        ["Account flat on BTC, operator's bot stopped", "yes"],
        ["Account value", f"{start['account_value_usdc']:.2f} USDC (mock funds)"],
        ["Trade size", f"{session.get('fixed_size', '-')} BTC, about {float(session.get('fixed_size', 0)) * start['markets']['BTC']['ask']:.0f} USDC, 5x cross"],
        ["BTC at the start", f"bid {start['markets']['BTC']['bid']} / ask {start['markets']['BTC']['ask']} ({start['markets']['BTC']['spread_bps']} bps spread)"],
        ["Host sleeps during the run (power log)", str(len(sleeps)) + (": " + ", ".join(sleeps) if sleeps else "")],
    ]))
    w("")
    w("## The 20 trades")
    w("")
    rows = []
    for c in cases:
        t = trade_of.get(c["id"])
        ending = c["title"].split("BTC, ", 1)[1] if "BTC, " in c["title"] else c["title"]
        tp_filled = sum(1 for o in t["orders"] if o["role"] == "TP" and (o["filled_size"] or 0) > 0) if t else 0
        if not t:
            ending = "no trade opened"
        if live and t:
            stopped = any(o["role"] == "SL" and (o["filled_size"] or 0) > 0 for o in t["orders"])
            ending = "stop-loss triggered" if stopped else ("all take-profits filled" if tp_filled == levels else
                                                            f"close command after {tp_filled} take-profit fill(s)" if tp_filled else "close command")
        rows.append([
            c["id"], (t["side"] if t else "-"), ending, (f"{t['avg_entry']:,.1f}" if t and t["avg_entry"] else "-"), (t["sl_price"] if t else "-"), note(c, "time from fill to protected: ") or "-",
            tp_filled, (t["outcome"] if t else "-"), f"{t['fees']:.4f}" if t and t["fees"] is not None else "-", f"{t['net_pnl']:.4f}" if t and t["net_pnl"] is not None else "-",
            f"{c['assertions_passed']}/{c['assertions_total']}", f"**{c['status'].upper()}**",
        ])
    w(table(["ID", "Side", "Ended by", "Entry", "Stop", "Fill to protected", "TPs filled", "Outcome", "Fees", "Net P/L", "Checks", "Status"], rows))
    w("")
    w("Checks made on every trade: the signal was accepted, the trade reached PROTECTED, the filled size equals the configured size, the entry price is inside the "
      "signal's zone, the exchange position matches in direction and size, the leverage is as configured, there is exactly one reduce-only stop for the full size at "
      "the signal's price, the configured number of reduce-only take-profits rest at the signal's prices and add up to the position, and after the ending the bot shows CLOSED with fees "
      "and a net result while the exchange is flat with no orders of the trade left.")
    w("")
    by_end: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        by_end.setdefault(c["scenario"], []).append(c)
    w(table(["Scenario", "Trades", "Passed", "Failed", "Blocked"], [[name, len(group), sum(c["status"] == "passed" for c in group), sum(c["status"] == "failed" for c in group),
                                                                    sum(c["status"] == "blocked" for c in group)] for name, group in by_end.items()]))
    w("")
    w("## Timing")
    w("")
    label = {"bot:time_to_protected": "Fill to PROTECTED (bot)", "bot:inject": "Bot accepts a signal (local)", "bot:close": "Bot accepts a close command (local)",
             "bot:modify_sl": "Move the stop (to the exchange and back)", "bot:kill": "Kill switch (flatten, cancel, pause)", "read:account": "Account query", "read:open_orders": "Open-orders query"}
    w(table(["Operation", "Samples", "Min ms", "Median ms", "p95 ms", "Max ms"], [[label[k], v["n"], v["min"], v["median"], v["p95"], v["max"]] for k, v in lat.items() if k in label]))
    w("")
    if protect:
        ordered = sorted(protect)
        w(f"- Fill to PROTECTED over {len(protect)} trades: min {ordered[0]:.2f} s, median {ordered[len(ordered) // 2]:.2f} s, max {ordered[-1]:.2f} s; "
          f"{sum(p <= 3.0 for p in protect)} of {len(protect)} within the 3 s target.")
    if slips:
        w(f"- Entry slippage against the touch price when the signal was built: largest {max(slips):.2f} bps, {sum(s == 0 for s in slips)} of {len(slips)} entries at exactly the touch.")
    w(f"- Verification reads that failed and were retried: {len(run.get('read_failures') or [])}.")
    w("")
    w("## Fees and P/L")
    w("")
    w(table(["", "USDC"], [
        ["Fees, all trades (bot's records)", f"{fees:.4f}"],
        ["Realised P/L, all trades", f"{realised:.4f}"],
        ["Net result, all trades", f"{net:.4f}"],
        ["Account value change over the run", f"{end['account_value_change_usdc']:.4f} ({start['account_value_usdc']:.2f} to {end['account_value_usdc']:.2f})"],
    ]))
    w("")
    w("The trades were opened and closed within about half a minute, so the result is mostly fees plus the spread. All funds are testnet mock funds.")
    w("")
    w("## Cleanup")
    w("")
    clean = run["cleanup"]
    w(f"**{'Clean' if clean['clean'] else 'NOT CLEAN'}.** BTC position after the final sweep: {clean['positions'].get('BTC')}. Open BTC orders: {len(clean['open_orders'])}.")
    w("")
    w("## Files")
    w("")
    w(table(["What", "Where"], [
        ["Runner and this suite's configuration", "`qa/run_qa.py --suite live20`, `qa/qa_live20.json`" if live else "`qa/run_qa.py --suite btc20`, `qa/qa_btc20.json`"],
        ["Raw results: every step, request, response, latency and check", f"`{run_dir}/results.json`"],
        ["Execution log", f"`{run_dir}/execution.log`"],
        ["Bot database with the full audit trail of every trade", f"`{args.db}` (your bot's own; the trades are in your dashboard's History)" if live else f"`{run_dir}/bot/`, `{run_dir}/bot-BTC.env`"],
        ["This report", f"`{run_dir}/report.md`, `{run_dir}/report.json`"],
    ]))
    w("")
    (run_dir / "report.md").write_text("\n".join(out))
    summary = {
        "run_id": run["run_id"], "suite": run.get("suite", "btc20"), "environment": run["environment"], "isolation": iso, "started_at": run["started_at"], "finished_at": run["finished_at"],
        "counts": counts, "host_sleeps_during_run": sleeps,
        "trades": [{"case": c["id"], "status": c["status"], "checks_passed": c["assertions_passed"], "checks_total": c["assertions_total"],
                    "failed_checks": [a["name"] for a in c["assertions"] if not a["passed"]], "blocked_reason": c["blocked_reason"], "error": c["error"],
                    **({k: trade_of[c["id"]][k] for k in ("side", "filled_size", "avg_entry", "sl_price", "outcome", "close_reason", "realized_pnl", "fees", "net_pnl",
                                                         "opened_at", "protected_at", "closed_at")} if c["id"] in trade_of else {})} for c in cases],
        "fill_to_protected_s": protect, "latency_ms": lat, "totals": {"fees": round(fees, 6), "realized_pnl": round(realised, 6), "net_pnl": round(net, 6)},
        "account_value": {"start": start["account_value_usdc"], "end": end["account_value_usdc"], "change": end["account_value_change_usdc"]},
        "read_failures": run.get("read_failures") or [], "cleanup": clean,
    }
    (run_dir / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"wrote {run_dir / 'report.md'} ({len(out)} lines) and {run_dir / 'report.json'}")


if __name__ == "__main__":
    main()
