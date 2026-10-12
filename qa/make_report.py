"""Build the QA report (Markdown and JSON) from a run's recorded results.

    .venv/bin/python qa/make_report.py qa/results/run-20261011 --retry qa/results/retry-20261011

Reads ``results.json`` (written case by case by ``run_qa.py``), the optional
retry run, ``defects.json`` and the test bot's own database. Nothing here
changes a recorded result: a retry is reported beside the original, never
instead of it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def table(headers: list[str], rows: list[list[Any]]) -> str:
    cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows])


def bot_trades(run_dir: Path) -> list[dict[str, Any]]:
    db = run_dir / "bot" / "data" / "hlbot-testnet.sqlite3"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, coin, side, state, outcome, filled_size, avg_entry, sl_price, realized_pnl, fees, net_pnl, opened_at, protected_at, closed_at FROM trades ORDER BY id")]
    conn.close()
    return rows


def tally(cases: list[dict[str, Any]], key: str) -> list[list[Any]]:
    groups: dict[str, dict[str, int]] = {}
    for c in cases:
        g = groups.setdefault(c[key], {"planned": 0, "passed": 0, "failed": 0, "blocked": 0, "skipped": 0})
        g["planned"] += 1
        if c["status"] in g:
            g[c["status"]] += 1
    return [[name, g["planned"], g["passed"], g["failed"], g["blocked"], g["skipped"]] for name, g in groups.items()]


def failed_assertions(case: dict[str, Any]) -> str:
    return "; ".join(a["name"] for a in case["assertions"] if not a["passed"]) or "none"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir")
    parser.add_argument("--retry", default=None)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    run = load(run_dir / "results.json")
    retry = load(Path(args.retry) / "results.json") if args.retry else None
    defects = load(run_dir / "defects.json") if (run_dir / "defects.json").exists() else []
    cases, counts = run["cases"], run["counts"]
    by_id = {c["id"]: c for c in cases}
    retried = {c["id"]: c for c in retry["cases"] if c["status"] != "not_run"} if retry else {}
    trades = bot_trades(run_dir)
    trade_of = dict(zip(("TC13", "TC16", "TC17", "TC14", "TC18", "TC15"), trades))  # the order the bot sessions ran them in
    bot_fees = sum(t["fees"] or 0 for t in trades)
    direct_fees = sum(c["fees_usdc"] for c in cases if c["layer"] == "exchange")
    lat = run["latency_ms"]
    start, end = run["start"], run["end"]

    out: list[str] = []
    w = out.append
    w("# QA report: 20 trade test cases on the Hyperliquid testnet")
    w("")
    w(f"Run `{run['run_id']}` · {run['started_at'][:19]}Z to {run['finished_at'][:19]}Z · account `{run['account']}` · environment **testnet only**")
    w("")
    w("## Result")
    w("")
    w(f"**{counts['passed']} of {counts['planned']} planned cases passed, {counts['failed']} failed, {counts['blocked']} blocked, {counts['skipped']} skipped. "
      f"All {counts['executed']} were executed and every one has a recorded outcome.** Every test order and position was reconciled: the account ended flat "
      "with no open orders.")
    w("")
    w("The three failures are real records and stay failed:")
    w("")
    w("- **TC17** (bot, long BTC, partial take-profit): the host computer went to sleep during the case. The bot still closed three take-profit levels and resized "
      "the stop, but the harness's own check then hit an exchange read timeout, so the case ended in an error.")
    w("- **TC18** (bot, short ETH, partial take-profit): the market did not reach the first take-profit in 240 s, and the same read timeout hit the closing check.")
    w("- **TC20** (invalid orders): eight of nine invalid orders were rejected; the ninth, a second order reusing a client order id, was **accepted** by the exchange.")
    w("")
    if retried:
        w("Retries, run afterwards with the Mac kept awake, are reported separately and do not change the counts above: "
          + ", ".join(f"{cid} **{c['status']}** ({c['assertions_passed']}/{c['assertions_total']} assertions)" for cid, c in retried.items()) + ".")
        w("")
    w("Six defects were found; the serious one is operational: **the Mac running the bot sleeps after one idle minute on battery** (52 times in the six hours before the run). "
      "See [Defects](#defects).")
    w("")
    w("## What was tested, and how")
    w("")
    w("The application is a signal-driven bot: its only entry is a limit IOC order, protected by one stop-loss and a ladder of take-profits. It has no plain market or "
      "resting-limit entry. The matrix was therefore run on two layers:")
    w("")
    w(table(["Layer", "Cases", "How"], [
        ["Exchange", "TC01-TC12, TC19, TC20 a-i", "Orders sent directly through the bot's own exchange adapter (`hlbot/exchange/hyperliquid.py`), the code the bot signs and sends with"],
        ["Application", "TC13-TC18, TC20 j", "The bot itself, started with a temporary settings file and its own data folder, fed injected signals; the exchange was queried independently to check what the bot reported"],
    ]))
    w("")
    w("A market order on Hyperliquid is an IOC limit with a slippage cap; that is what cases 1 to 6 send (cap 1 %).")
    w("")
    w("**Isolation and safety checks before the first order** (the runner refuses to continue if any fails):")
    w("")
    iso = run["isolation"]
    w(table(["Check", "Result"], [
        ["Configured environment", f"`{iso['env']}` (operator's `.env`: `{iso['operator_env']}`)"],
        ["REST endpoint", f"`{iso['rest_url']}`"],
        ["WebSocket endpoint", f"`{iso['ws_url']}`"],
        ["Signing key authorised for the account", "yes" if run["signer_authorized"] else "NO"],
        ["Operator's bot stopped (one writer per account)", "yes"],
        ["Account flat on BTC, ETH and SOL at the start", "yes"],
        ["Account value at the start", f"{start['account_value_usdc']:.2f} USDC (mock funds)"],
        ["Fees charged by the exchange", f"taker {start['taker_fee'] * 100:.3f} %, maker {start['maker_fee'] * 100:.3f} %"],
    ]))
    w("")
    w(table(["Symbol", "Size decimals", "Max leverage", "Bid / ask at start", "Spread", "Exchange-layer test size", "Bot test size"], [
        [coin, m["instrument"]["sz_decimals"], f"{m['instrument']['max_leverage']}x", f"{m['bid']} / {m['ask']}", f"{m['spread_bps']} bps", m["direct_size"],
         run.get("bot_sessions", {}).get(coin, {}).get("fixed_size", "-")] for coin, m in start["markets"].items()]))
    w("")
    w(f"Test sizes are about {run['config']['direct_notional_usd']} USDC per exchange-layer order and about 66-70 USDC per bot trade (five take-profit legs, each above the "
      "10 USDC minimum order value). Position mode is one-way (Hyperliquid has no hedge mode), cross margin, 5x leverage for the bot trades. Every order carried a unique client order id.")
    w("")
    w("## Results by case")
    w("")
    rows = []
    for c in cases:
        extra = ""
        if c["id"] in retried:
            extra = f" · retry: {retried[c['id']]['status']}"
        rows.append([c["id"], c["scenario"], c["symbol"], c["layer"], f"**{c['status'].upper()}**{extra}", f"{c['assertions_passed']}/{c['assertions_total']}", c["title"]])
    w(table(["ID", "Scenario", "Symbol", "Layer", "Status", "Assertions passed", "Case"], rows))
    w("")
    w("### By symbol")
    w("")
    w(table(["Symbol", "Planned", "Passed", "Failed", "Blocked", "Skipped"], tally(cases, "symbol")))
    w("")
    w("### By scenario")
    w("")
    w(table(["Scenario", "Planned", "Passed", "Failed", "Blocked", "Skipped"], tally(cases, "scenario")))
    w("")
    w("## The three failed cases")
    w("")
    tc17, tc18, tc20 = by_id["TC17"], by_id["TC18"], by_id["TC20"]
    w("### TC17: bot, long BTC, take-profits closing the position in parts")
    w("")
    w(f"Recorded result: **failed** with `{tc17['error']}` after {tc17['assertions_passed']} of {tc17['assertions_total']} assertions had passed.")
    w("")
    w("What happened, from the bot's audit trail and the Mac's power log:")
    w("")
    w(table(["Time (UTC)", "Event"], [
        ["09:55:52", "Mac enters idle sleep (power log). It then cycles about 27 s asleep, 10 s awake, until 09:59:56"],
        ["09:55:53.37", "Entry filled: 0.00085 BTC at 82,354"],
        ["09:55:53.68", "Stop-loss request sent. No answer arrives"],
        ["09:56:23.54", "The request ends as `unknown_outcome: ReadTimeout`. The bot looks the order up by client id: it has been resting on the exchange all along"],
        ["09:56:24.49", "Trade PROTECTED: 30.8 s after the fill by the wall clock, 5.99 s by the bot's own clock, which stood still while the Mac slept"],
        ["10:00:07", "After the Mac wakes: TP1, TP2 and TP3 fills are processed, trade REDUCING, stop-loss resized from 0.00085 to 0.00034 in 0.9 s"],
        ["10:00:15", "The harness's independent position query times out (first request after four idle minutes). The case ends in an error"],
        ["10:00:17", "The harness's safety sweep cancels the remaining orders and closes the 0.00034 BTC. The bot sees its stop cancelled, re-places it within 1.0 s, then records the trade closed"],
    ]))
    w("")
    w("The position was protected on the exchange throughout. The bot did the right things whenever it was awake. The case failed because the host slept (defect D1) and "
      "because the harness's verification read was not retried (the adapter's behaviour is defect D4; the harness now retries reads).")
    if "TC17" in retried:
        r = retried["TC17"]
        reason = (r["blocked_reason"] or "").strip()
        reason = reason[:1].upper() + reason[1:] + ("." if reason else "")
        w("")
        w(f"Retry with the Mac kept awake: **{r['status']}**, {r['assertions_passed']}/{r['assertions_total']} assertions passed. {reason} "
          "Entry, stop and all five take-profits were verified on the exchange (protected 2.04 s after the fill), and the trade was closed by command.")
    w("")
    w("### TC18: bot, short ETH, take-profits closing the position in parts")
    w("")
    w(f"Recorded result: **failed** with `{tc18['error']}`. Before that the case was already blocked: {tc18['blocked_reason']}. The trade was opened and protected in 1.92 s, "
      "then closed by command; the read timeout hit the check after the close. Final state on the exchange: flat, no orders.")
    if "TC18" in retried:
        r = retried["TC18"]
        checks = {a["name"]: a for a in r["assertions"]}
        w("")
        w(f"Retry: **{r['status']}**, {r['assertions_passed']}/{r['assertions_total']} assertions passed. TP1 filled, and the partial closure was verified on the exchange itself:")
        w("")
        w(table(["Check", "Expected", "Actual"], [[name, checks[name]["expected"], checks[name]["actual"]] for name in (
            "trade moved to REDUCING", "remaining size is the filled size minus what the take-profits closed", "exchange position shrank to the remaining size",
            "stop-loss was resized to the remaining position", "the unfilled take-profit levels still rest", "no more take-profit size rests than the position holds") if name in checks]))
    w("")
    w("### TC20: invalid order parameters")
    w("")
    w(f"Recorded result: **failed**, {tc20['assertions_passed']} of {tc20['assertions_total']} assertions passed. Failed assertions: {failed_assertions(tc20)}.")
    w("")
    w("The duplicate client id is a genuine finding (defect D5): the exchange accepted the second order, and the first could then no longer be cancelled by that id. "
      "The 'left an order' assertion failed for the same reason; that order was removed by exchange order id minutes later, before the next ETH case.")
    w("")
    w("In the official run a flaw in the harness, caused by defect D6, kept it from recording the exchange's rejection texts for subtests a to f and h. "
      "They were rejected (nothing was left resting), and the retry recorded the reasons:")
    w("")
    source = retried.get("TC20", tc20)
    rej = []
    for a in source["assertions"]:
        actual = a["actual"]
        text = actual.get("text", actual) if isinstance(actual, dict) and "text" in actual else actual
        rej.append([a["name"], "PASS" if a["passed"] else "**FAIL**", text])
    w(table(["Subtest (retry run)" if "TC20" in retried else "Subtest", "Result", "Exchange or bot answer"], rej))
    w("")
    w("No subtest created a position.")
    w("")
    w("## Order execution and latency")
    w("")
    w("Measured from this Mac over its home connection, official run. Signed requests (place, modify, cancel) go to `/exchange`; the rest are reads.")
    w("")
    label = {"place": "Place order", "cancel": "Cancel by client id", "modify": "Modify in place", "order_status": "Order status query", "book": "Order book query",
             "fills": "Fills query", "bot:inject": "Bot: accept a signal (local)", "bot:close": "Bot: accept a close command (local)", "bot:modify_sl": "Bot: move the stop (to exchange and back)",
             "bot:kill": "Bot: kill switch (flatten, cancel, pause)", "bot:resume": "Bot: resume (local)", "bot:time_to_protected": "Bot: fill to PROTECTED"}
    w(table(["Operation", "Samples", "Min ms", "Median ms", "p95 ms", "Max ms"], [[label.get(k, k), v["n"], v["min"], v["median"], v["p95"], v["max"]] for k, v in lat.items() if k in label]))
    w("")
    w("- Signed requests take about 0.9 s each on this testnet connection; reads about 0.1 s.")
    w("- Fill to PROTECTED was 1.85 to 2.65 s in five of six bot trades. The sixth, 30.8 s, is the sleeping-host case (TC17).")
    w("- Slippage against the touch price at decision time was 0.00 bps in every market order and every bot entry: the books were deep enough for these sizes.")
    w("- Modify in place keeps the client order id and issues a new exchange order id (TC19).")
    if retry and retry.get("read_failures"):
        w(f"- Read timeouts: 2 in the official run, {len(retry['read_failures'])} in the retry, each on the first request after about four idle minutes (defect D4).")
    w("")
    w("## TP/SL and position reconciliation")
    w("")
    w("For each bot trade the exchange was queried directly and compared with the signal: one reduce-only stop for the full size at the signal's price, five reduce-only "
      "take-profits at the signal's prices whose sizes add up to the position, the configured leverage, and after the close a flat position with no orders left.")
    w("")
    rows = []
    how = {"TC13": "close command", "TC14": "stop moved, then close command", "TC15": "close command", "TC16": "kill switch, then resume", "TC17": "TP1-3 filled; rest closed by the harness sweep",
           "TC18": "close command (no TP reached)"}
    for cid in ("TC13", "TC14", "TC15", "TC16", "TC17", "TC18"):
        c, t = by_id[cid], trade_of.get(cid)
        took = next((n.split(": ")[1] for n in c["notes"] if n.startswith("time from fill to protected")), "-")
        rows.append([cid, f"{t['side']} {t['coin']}" if t else "-", t["filled_size"] if t else "-", t["avg_entry"] if t else "-", t["sl_price"] if t else "-", took, how[cid],
                     t["outcome"] if t else "-", f"{c['final_state'].get('position')} / {c['final_state'].get('open_orders')}", c["status"]])
    w(table(["Case", "Trade", "Size", "Entry", "Stop", "Fill to protected", "Ended by", "Outcome", "Position / orders after the case", "Status"], rows))
    w("")
    w("TC17's 'after the case' figures were taken at the moment of the error; the sweep closed them seconds later. In TC14 the stop was moved and exactly one stop, "
      "full size, at the new price was found on the exchange. In TC16 the kill switch left nothing open, paused the bot, and resume returned it to HEALTHY.")
    w("")
    w("## Fees and P/L")
    w("")
    w(table(["", "USDC"], [
        ["Fees, exchange-layer cases (from fills)", f"{direct_fees:.4f}"],
        ["Fees, bot trades (from the test bot's database, all six trades)", f"{bot_fees:.4f}"],
        ["Realised P/L, bot trades", f"{sum(t['realized_pnl'] or 0 for t in trades):.4f}"],
        ["Realised P/L, exchange-layer cases", f"{sum(c['realized_pnl_usdc'] for c in cases if c['layer'] == 'exchange'):.4f}"],
        ["Account value change over the official run", f"{end['account_value_change_usdc']:.4f} ({start['account_value_usdc']:.2f} to {end['account_value_usdc']:.2f})"],
    ] + ([["Account value change over the retries", f"{retry['end']['account_value_change_usdc']:.4f}"]] if retry else [])))
    w("")
    w("Per-case fees in `results.json` are missing for TC17 and TC18 because those cases ended in an error before the figure was read; the table above takes them from the "
      "test bot's database instead. All funds are testnet mock funds.")
    w("")
    w("## Cleanup")
    w("")
    clean = run["cleanup"]
    w(f"**{'Clean' if clean['clean'] else 'NOT CLEAN'}.** Positions after the final sweep: {clean['positions']}. Open orders on the test symbols: {len(clean['open_orders'])}. "
      + (f"The retry run also ended clean: positions {retry['cleanup']['positions']}, open orders {len(retry['cleanup']['open_orders'])}." if retry else ""))
    w("")
    w("One manual step was needed during the official run: the orphaned duplicate-id order from TC20 (ETH buy 0.0054 at 2,417.4, order id 62412703046) was cancelled by "
      "exchange order id while TC17 was running. The runner now does this itself.")
    w("")
    w("## Defects")
    w("")
    w(table(["ID", "Severity", "Area", "Defect", "Found in"], [[d["id"], d["severity"], d["area"], d["title"], d["found_in"]] for d in defects]))
    for d in defects:
        w("")
        w(f"### {d['id']} ({d['severity']}): {d['title']}")
        w("")
        w(f"**Evidence.** {d['evidence']}")
        w("")
        w(f"**Impact.** {d['impact']}")
        w("")
        w(f"**Reproduce.** {d['reproduce']}")
        w("")
        w(f"**Recommended fix.** {d['fix']}")
    w("")
    w("## Side effects on your account and bot")
    w("")
    w("- Your bot was stopped twice, for about 13 and 9 minutes, and restarted with your own `.env`. It is running again (testnet, HEALTHY).")
    w("- The test bot used its own database, so your dashboard's trade history and win statistics contain no test trades. **Day P/L does include the test fees**: the bot "
      "counts every fill on the account for the day's result and for the daily-loss limit, whoever placed the order. The tests cost about 0.54 USDC, 0.27 % of the account, "
      "against a 2 % limit.")
    w("- ETH and SOL leverage on the account is now 5x cross (the bot sets leverage before an entry). BTC was already 5x.")
    w("")
    w("## Not covered")
    w("")
    w("- A stop-loss actually triggering under test control (the harness cannot move the market). Your own BTC trade earlier today shows one: stop FILLED, take-profits cancelled.")
    w("- A partially filled IOC entry, liquidation, funding payments, isolated margin, HIP-3 markets, and anything on mainnet.")
    w("- Multi-take-profit partial closure on BTC under clean conditions: observed in the official TC17 only through the bot's own records, blocked by a quiet market in the retry.")
    w("")
    w("## Deliverables")
    w("")
    w(table(["What", "Where"], [
        ["Test runner (all 20 cases, safety checks, cleanup)", "`qa/run_qa.py`"],
        ["Test configuration", "`qa/qa_config.json`"],
        ["This report generator", "`qa/make_report.py`"],
        ["Report, Markdown and JSON", f"`{run_dir}/report.md`, `{run_dir}/report.json`"],
        ["Raw results: every step, request, response, latency and assertion", f"`{run_dir}/results.json`"],
        ["Execution log", f"`{run_dir}/execution.log`"],
        ["Defects", f"`{run_dir}/defects.json`"],
        ["Test bot evidence: database with the full audit trail, logs, the settings used per symbol", f"`{run_dir}/bot/`, `{run_dir}/bot-*.env`"],
        ["Retry run (TC17, TC18, TC20)", f"`{args.retry}/`" if args.retry else "-"],
    ]))
    w("")
    w("Re-run: stop the bot, then `.venv/bin/python qa/run_qa.py --preflight` (checks only) and `caffeinate -i .venv/bin/python qa/run_qa.py`.")
    w("")
    (run_dir / "report.md").write_text("\n".join(out))

    summary = {
        "run_id": run["run_id"], "exchange": run["exchange"], "environment": run["environment"], "isolation": run["isolation"], "started_at": run["started_at"],
        "finished_at": run["finished_at"], "counts": counts,
        "by_symbol": {r[0]: dict(zip(("planned", "passed", "failed", "blocked", "skipped"), r[1:])) for r in tally(cases, "symbol")},
        "by_scenario": {r[0]: dict(zip(("planned", "passed", "failed", "blocked", "skipped"), r[1:])) for r in tally(cases, "scenario")},
        "cases": [{k: c[k] for k in ("id", "scenario", "symbol", "layer", "title", "status", "assertions_passed", "assertions_total", "fees_usdc", "realized_pnl_usdc",
                                     "blocked_reason", "error", "final_state")} | {"failed_assertions": [a["name"] for a in c["assertions"] if not a["passed"]]} for c in cases],
        "retries": [{k: c[k] for k in ("id", "status", "assertions_passed", "assertions_total", "blocked_reason", "error")} for c in retried.values()],
        "latency_ms": lat, "fees_usdc": {"exchange_layer": round(direct_fees, 6), "bot_trades": round(bot_fees, 6)},
        "account_value": {"start": start["account_value_usdc"], "end": end["account_value_usdc"], "change": end["account_value_change_usdc"],
                          "change_in_retries": retry["end"]["account_value_change_usdc"] if retry else None},
        "cleanup": run["cleanup"], "defects": defects, "bot_trades": trades,
    }
    (run_dir / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"wrote {run_dir / 'report.md'} ({len(out)} lines) and {run_dir / 'report.json'}")


if __name__ == "__main__":
    main()
