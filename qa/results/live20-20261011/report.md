# Test report: 20 real trades on BTC through your running bot, Hyperliquid testnet

Run `20261011T111812Z` · 2026-10-11T11:18:15Z to 2026-10-11T11:28:20Z · account `0x0d84…49ab` · environment **testnet only**

## Result

**17 of 20 trades passed every check, 3 failed, 0 blocked, 0 skipped.** The bot opened 19 trades (10 long, 9 short). After the run the account was flat with no open orders.

Not passed:

- **LV12** (Live bot: short BTC from a channel-style signal): failed. filled size equals the configured size; one reduce-only stop-loss for the full size at the signal's stop price; 3 take-profit orders rest on the exchange, one per configured share; take-profit prices match the signal's first levels
- **LV16** (Live bot: short BTC from a channel-style signal): failed. signal led to a trade
- **LV20** (Live bot: short BTC from a channel-style signal): failed. filled size equals the configured size; exchange position matches the trade (direction and size); one reduce-only stop-loss for the full size at the signal's stop price; 3 take-profit orders rest on the exchange, one per configured share; take-profit prices match the signal's first levels; take-profit sizes add up to the position

## What the three failures mean

### LV12: the bot recorded the wrong fill size (a real defect, found by accident)

The exchange filled the entry for 0.0005 BTC, and the bot's own fill record says 0.0005. One step later the bot believed 0.00085 had filled, placed its stop for 0.00085 and take-profits adding up to 0.00085. The exchange cancelled the first take-profit as larger than the position. About a second later the bot's reconciliation noticed the mismatch, marked the trade UNRECONCILED and resized the stop to the real 0.0005. The position was covered by a stop the whole time, and it was closed flat.

Cause: **the order's client id had been used before.** The bot derives a client order id from the environment, the account, the message the signal came from, and the order's role. For injected test messages the message number is a counter that starts at 1 in every database. The earlier test runs today used separate databases on the same account, so their message numbers 1 to 20 produced the same ids that your bot produced for its own messages 2 to 19: **105 of the 128 orders in this run reused a client id.** The exchange accepts a reused id. When the bot then asked for the order's status by that id, the exchange once answered with the *older* order (0.00085, from the 10:52 test), and the bot took that as its own fill.

In the other trades the status lookup happened to return the right order, so this is a race, about 1 in 14 here.

What this means for you:

- **It cannot happen between your Telegram channel and these tests**: channel messages carry the channel's own id and message number.
- **It can happen after your database is replaced or reset**: the bot re-reads the last 50 channel messages and would derive the same ids as before for the same messages. It can also happen whenever two data folders are used on one account, which is what the test setup did.
- Trade 17 in your History shows size 0.00085; the true size was 0.0005. Its P/L is right (it comes from the fills).

Recommended fix (two parts): add a random installation id, created once per database, to the material the client id is derived from; and when confirming an entry, accept an order status only if its exchange order id and original size match the order that was sent, and never record more filled than was ordered.

### LV20: a partial fill, handled as designed

The entry was a limit IOC for 0.0005 BTC; the book gave 0.00025 at 82,525 and 0.00009 at 82,526, and the rest was cancelled. With your policy `EXEC_PARTIAL_FILL_POLICY=accept` the bot re-planned on the 0.00034 that filled: stop for 0.00034, and two take-profits (0.00014 and 0.0002) instead of three, because a third leg would have been under the 10 USDC minimum. The exchange matched all of that. The failed checks are the harness expecting the full size and three levels; the bot's behaviour is correct. This is the first partial IOC fill observed on the real exchange.

### LV16: the bot refused a repeated signal

The harness built a signal with the same side and zone (82,520 to 82,532) as one it had sent three minutes earlier, and the bot rejected it as `DUPLICATE: ... already acted on`. That is the duplicate protection working. No trade was opened, so this run made 19 trades, not 20.

## The aborted first attempt

A first attempt was stopped after three trades (trades 3, 4 and 5 in your History, 0.00085 BTC each): the harness expected five take-profit orders, but your bot is configured for three (`EXEC_TP_CLOSE_PERCENTS=40,30,30`) and placed three, correctly. The check was corrected and the 20 cases above were run afresh at 0.0005 BTC so that the whole run stayed inside the day's loss limit. Its files are in `qa/results/live20-attempt1-aborted/`.

## How it was run

Each trade went through your own running bot, with your own settings (three take-profit shares of [40.0, 30.0, 30.0], your stop-distance and reward limits): a signal shaped like your channel's posts was injected, the bot entered with its limit IOC order and placed the stop-loss and take-profits. The trade was then held for up to 25 s, in which the market could reach a take-profit or the stop, and closed by command if still open. After every step the exchange was queried directly and compared with what the signal asked for. The harness placed, cancelled and closed nothing itself: your bot was the only writer on the account. The Mac was kept awake with `caffeinate`.

| Check before the first order | Result |
|---|---|
| Environment and endpoints | `testnet` · `https://api.hyperliquid-testnet.xyz` · `wss://api.hyperliquid-testnet.xyz/ws` |
| Signing key authorised | yes |
| Account flat on BTC, operator's bot stopped | yes |
| Account value | 197.33 USDC (mock funds) |
| Trade size | 0.0005 BTC, about 41 USDC, 5x cross |
| BTC at the start | bid 82418.0 / ask 82427.0 (1.09 bps spread) |
| Host sleeps during the run (power log) | 0 |

## The 20 trades

| ID | Side | Ended by | Entry | Stop | Fill to protected | TPs filled | Outcome | Fees | Net P/L | Checks | Status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| LV01 | BUY | close command | 82,427.0 | 82412.0 | 2.33 s | 0 | MANUAL | 0.0371 | -0.0411 | 16/16 | **PASSED** |
| LV02 | SELL | stop-loss triggered | 82,418.0 | 82432.0 | 2.15 s | 0 | LOSS_ALL | 0.0371 | -0.0540 | 17/17 | **PASSED** |
| LV03 | BUY | close command | 82,464.0 | 82447.0 | 1.92 s | 0 | MANUAL | 0.0371 | -0.0376 | 16/16 | **PASSED** |
| LV04 | SELL | stop-loss triggered | 82,469.0 | 82477.0 | 1.82 s | 0 | LOSS_ALL | 0.0371 | -0.0426 | 17/17 | **PASSED** |
| LV05 | BUY | close command after 1 take-profit fill(s) | 82,482.0 | 82468.0 | 1.92 s | 1 | MANUAL | 0.0322 | -0.0213 | 19/19 | **PASSED** |
| LV06 | SELL | close command | 82,505.0 | 82520.0 | 1.80 s | 0 | MANUAL | 0.0371 | -0.0421 | 16/16 | **PASSED** |
| LV07 | BUY | close command | 82,520.0 | 82504.0 | 2.02 s | 0 | MANUAL | 0.0371 | -0.0336 | 16/16 | **PASSED** |
| LV08 | SELL | close command | 82,527.0 | 82542.0 | 1.80 s | 0 | MANUAL | 0.0371 | -0.0376 | 16/16 | **PASSED** |
| LV09 | BUY | close command | 82,528.0 | 82510.0 | 1.89 s | 0 | MANUAL | 0.0371 | -0.0376 | 16/16 | **PASSED** |
| LV10 | SELL | close command | 82,527.0 | 82543.0 | 2.24 s | 0 | MANUAL | 0.0371 | -0.0376 | 16/16 | **PASSED** |
| LV11 | BUY | close command | 82,528.0 | 82513.0 | 2.20 s | 0 | MANUAL | 0.0371 | -0.0399 | 16/16 | **PASSED** |
| LV12 | SELL | close command | 82,519.0 | 82535.0 | 1.99 s | 0 | MANUAL | 0.0371 | -0.0411 | 12/16 | **FAILED** |
| LV13 | BUY | close command | 82,528.0 | 82511.0 | 2.01 s | 0 | MANUAL | 0.0371 | -0.0416 | 16/16 | **PASSED** |
| LV14 | SELL | close command | 82,524.0 | 82540.0 | 2.02 s | 0 | MANUAL | 0.0371 | -0.0391 | 16/16 | **PASSED** |
| LV15 | BUY | close command | 82,528.0 | 82514.0 | 1.85 s | 0 | MANUAL | 0.0371 | -0.0386 | 16/16 | **PASSED** |
| LV16 | - | no trade opened | - | - | - | 0 | - | - | - | 1/2 | **FAILED** |
| LV17 | BUY | close command | 82,528.0 | 82512.0 | 1.85 s | 0 | MANUAL | 0.0371 | -0.0407 | 16/16 | **PASSED** |
| LV18 | SELL | close command | 82,520.0 | 82537.0 | 1.79 s | 0 | MANUAL | 0.0371 | -0.0411 | 16/16 | **PASSED** |
| LV19 | BUY | close command | 82,528.0 | 82510.0 | 2.29 s | 0 | MANUAL | 0.0371 | -0.0381 | 16/16 | **PASSED** |
| LV20 | SELL | close command | 82,525.3 | 82540.0 | 1.79 s | 0 | MANUAL | 0.0253 | -0.0218 | 10/16 | **FAILED** |

Checks made on every trade: the signal was accepted, the trade reached PROTECTED, the filled size equals the configured size, the entry price is inside the signal's zone, the exchange position matches in direction and size, the leverage is as configured, there is exactly one reduce-only stop for the full size at the signal's price, the configured number of reduce-only take-profits rest at the signal's prices and add up to the position, and after the ending the bot shows CLOSED with fees and a net result while the exchange is flat with no orders of the trade left.

| Scenario | Trades | Passed | Failed | Blocked |
|---|---|---|---|---|
| Long through the live bot | 10 | 10 | 0 | 0 |
| Short through the live bot | 10 | 7 | 3 | 0 |

## Timing

| Operation | Samples | Min ms | Median ms | p95 ms | Max ms |
|---|---|---|---|---|---|
| Bot accepts a close command (local) | 19 | 0.9 | 8.5 | 14.9 | 14.9 |
| Bot accepts a signal (local) | 20 | 2.9 | 7.2 | 48.7 | 48.7 |
| Fill to PROTECTED (bot) | 19 | 1790.0 | 1923.0 | 2329.0 | 2329.0 |
| Account query | 41 | 107.8 | 118.1 | 266.4 | 497.6 |
| Open-orders query | 41 | 105.3 | 109.3 | 192.2 | 1151.3 |

- Fill to PROTECTED over 19 trades: min 1.79 s, median 1.92 s, max 2.33 s; 19 of 19 within the 3 s target.
- Entry slippage against the touch price when the signal was built: largest 0.73 bps, 17 of 19 entries at exactly the touch.
- Verification reads that failed and were retried: 0.

## Fees and P/L

|  | USDC |
|---|---|
| Fees, all trades (bot's records) | 0.6886 |
| Realised P/L, all trades | -0.0388 |
| Net result, all trades | -0.7274 |
| Account value change over the run | -0.7274 (197.33 to 196.61) |

The trades were opened and closed within about half a minute, so the result is mostly fees plus the spread. All funds are testnet mock funds.

## Cleanup

**Clean.** BTC position after the final sweep: 0.0. Open BTC orders: 0.

## Files

| What | Where |
|---|---|
| Runner and this suite's configuration | `qa/run_qa.py --suite live20`, `qa/qa_live20.json` |
| Raw results: every step, request, response, latency and check | `qa/results/live20-20261011/results.json` |
| Execution log | `qa/results/live20-20261011/execution.log` |
| Bot database with the full audit trail of every trade | `data/hlbot-testnet.sqlite3` (your bot's own; the trades are in your dashboard's History) |
| This report | `qa/results/live20-20261011/report.md`, `qa/results/live20-20261011/report.json` |
