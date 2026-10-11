# Test report: 20 bot trades on BTC, Hyperliquid testnet

Run `20261011T104603Z` · 2026-10-11T10:46:05Z to 2026-10-11T10:53:34Z · account `0x0d84…49ab` · environment **testnet only**

## Result

**18 of 20 trades passed every check, 0 failed, 2 blocked, 0 skipped.** The bot opened 20 trades (10 long, 10 short). After the run the account was flat with no open orders.

Not passed:

- **BT09** (Bot: long BTC, take-profit levels a few ticks away; waits for a partial close, then closes the rest): blocked. the market did not reach the first take-profit (82342.0) within 120 s, so partial closure could not be observed
- **BT10** (Bot: short BTC, take-profit levels a few ticks away; waits for a partial close, then closes the rest): blocked. the market did not reach the first take-profit (82292.0) within 120 s, so partial closure could not be observed

## How it was run

Each trade went through the bot itself: a signal in the channel's format was injected, the bot entered with its limit IOC order, placed one stop-loss and five take-profits, and the trade was ended in one of four ways. After every step the exchange was queried directly and compared with what the signal asked for. The bot ran with a temporary settings file and its own data folder; the operator's bot was stopped for the duration. The Mac was kept awake with `caffeinate`.

| Check before the first order | Result |
|---|---|
| Environment and endpoints | `testnet` · `https://api.hyperliquid-testnet.xyz` · `wss://api.hyperliquid-testnet.xyz/ws` |
| Signing key authorised | yes |
| Account flat on BTC, operator's bot stopped | yes |
| Account value | 198.93 USDC (mock funds) |
| Trade size | 0.00085 BTC, about 70 USDC, 5x cross |
| BTC at the start | bid 82316.0 / ask 82324.0 (0.97 bps spread) |
| Host sleeps during the run (power log) | 0 |

## The 20 trades

| ID | Side | Ended by | Entry | Stop | Fill to protected | TPs filled | Outcome | Fees | Net P/L | Checks | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BT01 | BUY | closed by command | 82,325.4 | 81583.0 | 1.76 s | 0 | MANUAL | 0.0630 | -0.0701 | 16/16 | **PASSED** |
| BT02 | SELL | closed by command | 82,316.0 | 83057.0 | 1.86 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |
| BT03 | BUY | closed by command | 82,325.0 | 81584.0 | 1.98 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |
| BT04 | SELL | closed by command | 82,316.0 | 83057.0 | 2.00 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |
| BT05 | BUY | stop-loss moved, then closed by command | 82,325.0 | 81831.0 | 2.06 s | 0 | MANUAL | 0.0630 | -0.0698 | 19/19 | **PASSED** |
| BT06 | SELL | stop-loss moved, then closed by command | 82,316.0 | 82810.0 | 2.20 s | 0 | MANUAL | 0.0630 | -0.0630 | 19/19 | **PASSED** |
| BT07 | BUY | closed by command | 82,316.0 | 81575.0 | 2.20 s | 0 | MANUAL | 0.0630 | -0.0693 | 16/16 | **PASSED** |
| BT08 | SELL | closed by command | 82,306.0 | 83047.0 | 2.48 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |
| BT09 | BUY | take-profit levels a few ticks away; waits for a partial close, then closes the rest | 82,315.0 | 81739.0 | 1.80 s | 0 | MANUAL | 0.0630 | -0.0579 | 7/7 | **BLOCKED** |
| BT10 | SELL | take-profit levels a few ticks away; waits for a partial close, then closes the rest | 82,320.0 | 82896.0 | 1.93 s | 0 | MANUAL | 0.0630 | -0.0714 | 7/7 | **BLOCKED** |
| BT11 | BUY | closed by command | 82,331.0 | 81590.0 | 1.89 s | 0 | MANUAL | 0.0630 | -0.0655 | 16/16 | **PASSED** |
| BT12 | SELL | closed by command | 82,327.0 | 83068.0 | 2.00 s | 0 | MANUAL | 0.0630 | -0.0706 | 16/16 | **PASSED** |
| BT13 | BUY | closed by the kill switch, then resumed | 82,337.0 | 81596.0 | 2.00 s | 0 | FLATTENED | 0.0630 | -0.0706 | 18/18 | **PASSED** |
| BT14 | SELL | closed by the kill switch, then resumed | 82,327.0 | 83068.0 | 1.98 s | 0 | FLATTENED | 0.0630 | -0.0706 | 18/18 | **PASSED** |
| BT15 | BUY | closed by command | 82,337.0 | 81596.0 | 1.88 s | 0 | MANUAL | 0.0630 | -0.0706 | 16/16 | **PASSED** |
| BT16 | SELL | closed by command | 82,328.0 | 83069.0 | 2.03 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |
| BT17 | BUY | stop-loss moved, then closed by command | 82,337.0 | 81843.0 | 1.97 s | 0 | MANUAL | 0.0630 | -0.0706 | 19/19 | **PASSED** |
| BT18 | SELL | stop-loss moved, then closed by command | 82,327.0 | 82821.0 | 2.00 s | 0 | MANUAL | 0.0630 | -0.0706 | 19/19 | **PASSED** |
| BT19 | BUY | closed by command | 82,337.0 | 81596.0 | 1.88 s | 0 | MANUAL | 0.0630 | -0.0706 | 16/16 | **PASSED** |
| BT20 | SELL | closed by command | 82,328.0 | 83069.0 | 1.84 s | 0 | MANUAL | 0.0630 | -0.0698 | 16/16 | **PASSED** |

Checks made on every trade: the signal was accepted, the trade reached PROTECTED, the filled size equals the configured size, the entry price is inside the signal's zone, the exchange position matches in direction and size, the leverage is as configured, there is exactly one reduce-only stop for the full size at the signal's price, five reduce-only take-profits rest at the signal's prices and add up to the position, and after the ending the bot shows CLOSED with fees and a net result while the exchange is flat with no orders of the trade left.

| Scenario | Trades | Passed | Failed | Blocked |
|---|---|---|---|---|
| Long with TP/SL | 6 | 6 | 0 | 0 |
| Short with TP/SL | 6 | 6 | 0 | 0 |
| Stop-loss moved | 4 | 4 | 0 | 0 |
| Multiple TP / partial close | 2 | 0 | 0 | 2 |
| Kill switch | 2 | 2 | 0 | 0 |

## Timing

| Operation | Samples | Min ms | Median ms | p95 ms | Max ms |
|---|---|---|---|---|---|
| Bot accepts a close command (local) | 18 | 1.6 | 4.8 | 22.9 | 22.9 |
| Bot accepts a signal (local) | 20 | 2.9 | 5.7 | 27.0 | 27.0 |
| Kill switch (flatten, cancel, pause) | 2 | 3451.9 | 3726.9 | 3726.9 | 3726.9 |
| Move the stop (to the exchange and back) | 4 | 870.0 | 927.2 | 937.8 | 937.8 |
| Fill to PROTECTED (bot) | 20 | 1762.0 | 1978.0 | 2482.0 | 2482.0 |
| Account query | 59 | 104.0 | 128.5 | 198.7 | 258.8 |
| Open-orders query | 44 | 104.6 | 109.2 | 188.4 | 191.8 |

- Fill to PROTECTED over 20 trades: min 1.76 s, median 1.98 s, max 2.48 s; 20 of 20 within the 3 s target.
- Entry slippage against the touch price when the signal was built: largest 0.17 bps, 17 of 18 entries at exactly the touch.
- Verification reads that failed and were retried: 2.

## Fees and P/L

|  | USDC |
|---|---|
| Fees, all trades (bot's records) | 1.2596 |
| Realised P/L, all trades | -0.1206 |
| Net result, all trades | -1.3801 |
| Account value change over the run | -1.3801 (198.93 to 197.55) |

The trades were opened and closed within seconds, so the result is fees plus the spread. All funds are testnet mock funds.

## Cleanup

**Clean.** BTC position after the final sweep: 0.0. Open BTC orders: 0.

## Files

| What | Where |
|---|---|
| Runner and this suite's configuration | `qa/run_qa.py --suite btc20`, `qa/qa_btc20.json` |
| Raw results: every step, request, response, latency and check | `qa/results/btc20-20261011/results.json` |
| Execution log | `qa/results/btc20-20261011/execution.log` |
| Test bot's database (full audit trail), logs and settings | `qa/results/btc20-20261011/bot/`, `qa/results/btc20-20261011/bot-BTC.env` |
| This report | `qa/results/btc20-20261011/report.md`, `qa/results/btc20-20261011/report.json` |
