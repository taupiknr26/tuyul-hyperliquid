# QA report: 20 trade test cases on the Hyperliquid testnet

Run `20261011T095415Z` · 2026-10-11T09:54:18Z to 2026-10-11T10:05:20Z · account `0x0d84…49ab` · environment **testnet only**

## Result

**17 of 20 planned cases passed, 3 failed, 0 blocked, 0 skipped. All 20 were executed and every one has a recorded outcome.** Every test order and position was reconciled: the account ended flat with no open orders.

The three failures are real records and stay failed:

- **TC17** (bot, long BTC, partial take-profit): the host computer went to sleep during the case. The bot still closed three take-profit levels and resized the stop, but the harness's own check then hit an exchange read timeout, so the case ended in an error.
- **TC18** (bot, short ETH, partial take-profit): the market did not reach the first take-profit in 240 s, and the same read timeout hit the closing check.
- **TC20** (invalid orders): eight of nine invalid orders were rejected; the ninth, a second order reusing a client order id, was **accepted** by the exchange.

Retries, run afterwards with the Mac kept awake, are reported separately and do not change the counts above: TC17 **blocked** (7/7 assertions), TC18 **passed** (15/15 assertions), TC20 **failed** (12/13 assertions).

Six defects were found; the serious one is operational: **the Mac running the bot sleeps after one idle minute on battery** (52 times in the six hours before the run). See [Defects](#defects).

## What was tested, and how

The application is a signal-driven bot: its only entry is a limit IOC order, protected by one stop-loss and a ladder of take-profits. It has no plain market or resting-limit entry. The matrix was therefore run on two layers:

| Layer | Cases | How |
|---|---|---|
| Exchange | TC01-TC12, TC19, TC20 a-i | Orders sent directly through the bot's own exchange adapter (`hlbot/exchange/hyperliquid.py`), the code the bot signs and sends with |
| Application | TC13-TC18, TC20 j | The bot itself, started with a temporary settings file and its own data folder, fed injected signals; the exchange was queried independently to check what the bot reported |

A market order on Hyperliquid is an IOC limit with a slippage cap; that is what cases 1 to 6 send (cap 1 %).

**Isolation and safety checks before the first order** (the runner refuses to continue if any fails):

| Check | Result |
|---|---|
| Configured environment | `testnet` (operator's `.env`: `testnet`) |
| REST endpoint | `https://api.hyperliquid-testnet.xyz` |
| WebSocket endpoint | `wss://api.hyperliquid-testnet.xyz/ws` |
| Signing key authorised for the account | yes |
| Operator's bot stopped (one writer per account) | yes |
| Account flat on BTC, ETH and SOL at the start | yes |
| Account value at the start | 199.47 USDC (mock funds) |
| Fees charged by the exchange | taker 0.045 %, maker 0.015 % |

| Symbol | Size decimals | Max leverage | Bid / ask at start | Spread | Exchange-layer test size | Bot test size |
|---|---|---|---|---|---|---|
| BTC | 5 | 40x | 82344.0 / 82353.0 | 1.09 bps | 0.00016 | 0.00085 |
| ETH | 4 | 25x | 2492.2 / 2492.5 | 1.2 bps | 0.0053 | 0.0265 |
| SOL | 2 | 10x | 108.9 / 108.95 | 4.59 bps | 0.12 | 0.65 |

Test sizes are about 13 USDC per exchange-layer order and about 66-70 USDC per bot trade (five take-profit legs, each above the 10 USDC minimum order value). Position mode is one-way (Hyperliquid has no hedge mode), cross margin, 5x leverage for the bot trades. Every order carried a unique client order id.

## Results by case

| ID | Scenario | Symbol | Layer | Status | Assertions passed | Case |
|---|---|---|---|---|---|---|
| TC01 | Market buy | BTC | exchange | **PASSED** | 10/10 | Market buy BTC |
| TC02 | Market buy | ETH | exchange | **PASSED** | 10/10 | Market buy ETH |
| TC03 | Market buy | SOL | exchange | **PASSED** | 10/10 | Market buy SOL |
| TC04 | Market sell | BTC | exchange | **PASSED** | 10/10 | Market sell BTC (open short) |
| TC05 | Market sell | ETH | exchange | **PASSED** | 10/10 | Market sell ETH (open short) |
| TC06 | Market sell | SOL | exchange | **PASSED** | 10/10 | Market sell SOL (open short) |
| TC07 | Limit buy | BTC | exchange | **PASSED** | 10/10 | Limit buy BTC, resting below the market |
| TC08 | Limit buy | ETH | exchange | **PASSED** | 9/9 | Limit buy ETH at the touch |
| TC09 | Limit buy | SOL | exchange | **PASSED** | 10/10 | Limit buy SOL, post-only |
| TC10 | Limit sell | BTC | exchange | **PASSED** | 9/9 | Limit sell BTC at the touch |
| TC11 | Limit sell | ETH | exchange | **PASSED** | 10/10 | Limit sell ETH, resting above the market |
| TC12 | Limit sell | SOL | exchange | **PASSED** | 10/10 | Limit sell SOL, post-only |
| TC13 | Long with TP/SL | BTC | bot | **PASSED** | 16/16 | Bot: long BTC from a signal, closed by command |
| TC14 | Long with TP/SL | ETH | bot | **PASSED** | 19/19 | Bot: long ETH, stop-loss moved, then closed |
| TC15 | Short with TP/SL | SOL | bot | **PASSED** | 16/16 | Bot: short SOL from a signal, closed by command |
| TC16 | Short with TP/SL | BTC | bot | **PASSED** | 18/18 | Bot: short BTC, closed by the kill switch |
| TC17 | Multiple TP / partial close | BTC | bot | **FAILED** · retry: blocked | 5/5 | Bot: long BTC, take-profit levels close the position in parts |
| TC18 | Multiple TP / partial close | ETH | bot | **FAILED** · retry: passed | 4/4 | Bot: short ETH, take-profit levels close the position in parts |
| TC19 | Cancel and replace | BTC | exchange | **PASSED** | 11/11 | Modify in place, cancel, and replace a resting order |
| TC20 | Invalid parameters | ETH | exchange+bot | **FAILED** · retry: failed | 11/13 | Rejected orders: nine exchange subtests and one application subtest |

### By symbol

| Symbol | Planned | Passed | Failed | Blocked | Skipped |
|---|---|---|---|---|---|
| BTC | 8 | 7 | 1 | 0 | 0 |
| ETH | 7 | 5 | 2 | 0 | 0 |
| SOL | 5 | 5 | 0 | 0 | 0 |

### By scenario

| Scenario | Planned | Passed | Failed | Blocked | Skipped |
|---|---|---|---|---|---|
| Market buy | 3 | 3 | 0 | 0 | 0 |
| Market sell | 3 | 3 | 0 | 0 | 0 |
| Limit buy | 3 | 3 | 0 | 0 | 0 |
| Limit sell | 3 | 3 | 0 | 0 | 0 |
| Long with TP/SL | 2 | 2 | 0 | 0 | 0 |
| Short with TP/SL | 2 | 2 | 0 | 0 | 0 |
| Multiple TP / partial close | 2 | 0 | 2 | 0 | 0 |
| Cancel and replace | 1 | 1 | 0 | 0 | 0 |
| Invalid parameters | 1 | 0 | 1 | 0 | 0 |

## The three failed cases

### TC17: bot, long BTC, take-profits closing the position in parts

Recorded result: **failed** with `OutcomeUnknown: unknown_outcome: ReadTimeout` after 5 of 5 assertions had passed.

What happened, from the bot's audit trail and the Mac's power log:

| Time (UTC) | Event |
|---|---|
| 09:55:52 | Mac enters idle sleep (power log). It then cycles about 27 s asleep, 10 s awake, until 09:59:56 |
| 09:55:53.37 | Entry filled: 0.00085 BTC at 82,354 |
| 09:55:53.68 | Stop-loss request sent. No answer arrives |
| 09:56:23.54 | The request ends as `unknown_outcome: ReadTimeout`. The bot looks the order up by client id: it has been resting on the exchange all along |
| 09:56:24.49 | Trade PROTECTED: 30.8 s after the fill by the wall clock, 5.99 s by the bot's own clock, which stood still while the Mac slept |
| 10:00:07 | After the Mac wakes: TP1, TP2 and TP3 fills are processed, trade REDUCING, stop-loss resized from 0.00085 to 0.00034 in 0.9 s |
| 10:00:15 | The harness's independent position query times out (first request after four idle minutes). The case ends in an error |
| 10:00:17 | The harness's safety sweep cancels the remaining orders and closes the 0.00034 BTC. The bot sees its stop cancelled, re-places it within 1.0 s, then records the trade closed |

The position was protected on the exchange throughout. The bot did the right things whenever it was awake. The case failed because the host slept (defect D1) and because the harness's verification read was not retried (the adapter's behaviour is defect D4; the harness now retries reads).

Retry with the Mac kept awake: **blocked**, 7/7 assertions passed. The market did not reach the first take-profit (82371.0) within 240 s, so partial closure could not be observed. Entry, stop and all five take-profits were verified on the exchange (protected 2.04 s after the fill), and the trade was closed by command.

### TC18: bot, short ETH, take-profits closing the position in parts

Recorded result: **failed** with `OutcomeUnknown: unknown_outcome: ReadTimeout`. Before that the case was already blocked: the market did not reach the first take-profit (2492.5) within 240 s, so partial closure could not be observed. The trade was opened and protected in 1.92 s, then closed by command; the read timeout hit the check after the close. Final state on the exchange: flat, no orders.

Retry: **passed**, 15/15 assertions passed. TP1 filled, and the partial closure was verified on the exchange itself:

| Check | Expected | Actual |
|---|---|---|
| trade moved to REDUCING | REDUCING | REDUCING |
| remaining size is the filled size minus what the take-profits closed | 0.0212 | 0.0212 |
| exchange position shrank to the remaining size | 0.0212 | 0.0212 |
| stop-loss was resized to the remaining position | 0.0212 | [0.0212] |
| the unfilled take-profit levels still rest | 4 | 4 |
| no more take-profit size rests than the position holds | <= 0.0212 | 0.0212 |

### TC20: invalid order parameters

Recorded result: **failed**, 11 of 13 assertions passed. Failed assertions: i. second order with the same client id: rejected; no subtest created a position or left an order.

The duplicate client id is a genuine finding (defect D5): the exchange accepted the second order, and the first could then no longer be cancelled by that id. The 'left an order' assertion failed for the same reason; that order was removed by exchange order id minutes later, before the next ETH case.

In the official run a flaw in the harness, caused by defect D6, kept it from recording the exchange's rejection texts for subtests a to f and h. They were rejected (nothing was left resting), and the retry recorded the reasons:

| Subtest (retry run) | Result | Exchange or bot answer |
|---|---|---|
| a. order value below the 10 USDC minimum: rejected | PASS | local_bug: Order must have minimum value of $10. asset=4 |
| b. reduce-only sell with no position: rejected | PASS | reduce_only: Reduce only order would increase position. asset=4 |
| c. price off the tick grid: rejected | PASS | other: Order has invalid price. |
| d. size with too many decimals: rejected | PASS | local_bug: Order has invalid size. |
| e. zero size: rejected | PASS | other: Order has zero size. |
| f. price ten times the market: rejected | PASS | other: Order price cannot be more than 80% away from the reference price |
| g. unknown symbol: rejected | PASS | local_bug: asset id for NOPE has not been resolved |
| h. post-only buy priced through the ask: rejected | PASS | other: Post only order would have immediately matched, bbo was 2491.7@2492. asset=4 |
| i. second order with the same client id: rejected | **FAIL** | {'status': 'resting', 'oid': 62414156719, 'total_size': None, 'avg_price': None, 'error': None} |
| i. still exactly one order under that client id | PASS | 1 |
| no subtest created a position or left an order | PASS | {'position': 0.0, 'open_orders': 0} |
| j. bot: a buy signal with its stop above the entry is rejected as INVALID | PASS | {'state': 'INVALID', 'reason': 'entry_zone.low must be less than entry_zone.high; stop_loss must be below entry_zone for a BUY'} |
| j. bot: the invalid signal produced no order and no position | PASS | {'open_orders': 0, 'position': 0.0} |

No subtest created a position.

## Order execution and latency

Measured from this Mac over its home connection, official run. Signed requests (place, modify, cancel) go to `/exchange`; the rest are reads.

| Operation | Samples | Min ms | Median ms | p95 ms | Max ms |
|---|---|---|---|---|---|
| Order book query | 20 | 96.2 | 146.9 | 282.8 | 282.8 |
| Bot: accept a close command (local) | 4 | 2.6 | 9.7 | 12.5 | 12.5 |
| Bot: accept a signal (local) | 7 | 5.9 | 9.6 | 13.9 | 13.9 |
| Bot: kill switch (flatten, cancel, pause) | 1 | 4060.4 | 4060.4 | 4060.4 | 4060.4 |
| Bot: move the stop (to exchange and back) | 1 | 825.5 | 825.5 | 825.5 | 825.5 |
| Bot: resume (local) | 1 | 6.3 | 6.3 | 6.3 | 6.3 |
| Bot: fill to PROTECTED | 6 | 1854.0 | 2429.0 | 30824.0 | 30824.0 |
| Cancel by client id | 13 | 818.4 | 920.0 | 1137.4 | 1137.4 |
| Fills query | 8 | 94.9 | 116.4 | 243.4 | 243.4 |
| Modify in place | 1 | 887.4 | 887.4 | 887.4 | 887.4 |
| Order status query | 20 | 95.8 | 104.9 | 518.1 | 518.1 |
| Place order | 33 | 0.0 | 866.2 | 1297.2 | 1890.0 |

- Signed requests take about 0.9 s each on this testnet connection; reads about 0.1 s.
- Fill to PROTECTED was 1.85 to 2.65 s in five of six bot trades. The sixth, 30.8 s, is the sleeping-host case (TC17).
- Slippage against the touch price at decision time was 0.00 bps in every market order and every bot entry: the books were deep enough for these sizes.
- Modify in place keeps the client order id and issues a new exchange order id (TC19).
- Read timeouts: 2 in the official run, 2 in the retry, each on the first request after about four idle minutes (defect D4).

## TP/SL and position reconciliation

For each bot trade the exchange was queried directly and compared with the signal: one reduce-only stop for the full size at the signal's price, five reduce-only take-profits at the signal's prices whose sizes add up to the position, the configured leverage, and after the close a flat position with no orders left.

| Case | Trade | Size | Entry | Stop | Fill to protected | Ended by | Outcome | Position / orders after the case | Status |
|---|---|---|---|---|---|---|---|---|---|
| TC13 | BUY BTC | 0.00085 | 82351.0 | 81610.0 | 2.43 s | close command | MANUAL | 0.0 / 0 | passed |
| TC14 | BUY ETH | 0.0265 | 2493.5 | 2478.5 | 1.90 s | stop moved, then close command | MANUAL | 0.0 / 0 | passed |
| TC15 | SELL SOL | 0.65 | 109.09 | 110.07 | 1.85 s | close command | MANUAL | 0.0 / 0 | passed |
| TC16 | SELL BTC | 0.00085 | 82347.00000000001 | 83088.0 | 2.65 s | kill switch, then resume | FLATTENED | 0.0 / 0 | passed |
| TC17 | BUY BTC | 0.00085 | 82354.0 | 81778.0 | 30.82 s | TP1-3 filled; rest closed by the harness sweep | MANUAL | 0.00034 / 3 | failed |
| TC18 | SELL ETH | 0.0265 | 2493.1999999999994 | 2510.7 | 1.92 s | close command (no TP reached) | MANUAL | 0.0 / 0 | failed |

TC17's 'after the case' figures were taken at the moment of the error; the sweep closed them seconds later. In TC14 the stop was moved and exactly one stop, full size, at the new price was found on the exchange. In TC16 the kill switch left nothing open, paused the bot, and resume returned it to HEALTHY.

## Fees and P/L

|  | USDC |
|---|---|
| Fees, exchange-layer cases (from fills) | 0.0948 |
| Fees, bot trades (from the test bot's database, all six trades) | 0.3592 |
| Realised P/L, bot trades | -0.0119 |
| Realised P/L, exchange-layer cases | -0.0195 |
| Account value change over the official run | -0.4231 (199.47 to 199.05) |
| Account value change over the retries | -0.1203 |

Per-case fees in `results.json` are missing for TC17 and TC18 because those cases ended in an error before the figure was read; the table above takes them from the test bot's database instead. All funds are testnet mock funds.

## Cleanup

**Clean.** Positions after the final sweep: {'BTC': 0.0, 'ETH': 0.0, 'SOL': 0.0}. Open orders on the test symbols: 0. The retry run also ended clean: positions {'BTC': 0.0, 'ETH': 0.0, 'SOL': 0.0}, open orders 0.

One manual step was needed during the official run: the orphaned duplicate-id order from TC20 (ETH buy 0.0054 at 2,417.4, order id 62412703046) was cancelled by exchange order id while TC17 was running. The runner now does this itself.

## Defects

| ID | Severity | Area | Defect | Found in |
|---|---|---|---|---|
| D1 | Critical | Operations / host | The host computer goes to sleep while the bot is running | TC17 (official run) |
| D2 | Medium | Application | The bot does not notice that it was suspended | TC17 (official run) |
| D3 | Medium | Application / configuration | Price-point settings and the fixed size are global, not per symbol | Test setup for TC13-TC18 |
| D4 | Low | Exchange adapter | The first request after an idle period fails with a 5 s read timeout, and read-only queries are classified 'unknown outcome' | TC17 and TC18 (official run); again in the retries with sleep prevented |
| D5 | Low | Exchange behaviour (affects cancel logic) | The exchange accepts a second order with a client order id that is already in use, and the first order then cannot be cancelled by that id | TC20 subtest i (official run and retry) |
| D6 | Low | Exchange adapter | ExchangeError cannot be copied or pickled | TC20 (official run), through the test harness |

### D1 (Critical): The host computer goes to sleep while the bot is running

**Evidence.** macOS power log: 'Entering Sleep state due to Idle Sleep' at 16:55:52 local (09:55:52 UTC), then six sleep/wake cycles of about 27 s asleep and 10 s awake until 16:59:56; 52 sleeps in the six hours before the run. The Mac was on battery with idle sleep set to 1 minute. In TC17 the stop-loss request was sent at 09:55:53.676 and its answer never arrived (ReadTimeout); the bot recorded PROTECTED 30.8 s after the fill by the wall clock. The stop was in fact resting on the exchange from the start (resolved by client id). Three take-profit fills were only processed at 10:00:07, after the Mac woke.

**Impact.** While the host sleeps the bot manages nothing: no signal is read, no fill is processed, no stop is moved or re-placed, and the kill switch and dashboard do not answer. Orders already resting on the exchange keep working, which is why no position was ever unprotected here.

**Reproduce.** Run the bot on a Mac on battery with default power settings and leave the keyboard alone for a minute; watch `pmset -g log | grep -E 'Sleep|Wake'` and the bot's log for 'websocket silent' and 'market_data_stale'.

**Recommended fix.** Run the bot on an always-on host (the Docker image or systemd unit on a server). On this Mac, at minimum keep it on mains power and start the bot under `caffeinate -is`, or disable sleep with `sudo pmset -a sleep 0`.

### D2 (Medium): The bot does not notice that it was suspended

**Evidence.** The trade's own metric says 'trade.protected ms: 5989' while 30.8 s passed on the wall clock: the deadlines for protection (soft 3 s, hard 8 s) and the metric run on a clock that stands still during host sleep. The alerts raised afterwards ('market data stream is disconnected or silent', '1 order(s) unresolved past their deadline') describe symptoms, not the cause.

**Impact.** After a suspension the operator sees generic stale-data alerts and a time-to-protected figure that understates what happened. The deadlines are not wall-clock guarantees on a host that can sleep.

**Reproduce.** Suspend the process or the host for 20 s while a trade is open (`kill -STOP <pid>; sleep 20; kill -CONT <pid>`), then read the audit trail.

**Recommended fix.** In the watchdog, compare wall-clock and monotonic time each second; on a jump of more than a few seconds raise a 'host was suspended for N s' alert, run a reconciliation at once, and record wall-clock time-to-protected alongside the monotonic one.

### D3 (Medium): Price-point settings and the fixed size are global, not per symbol

**Evidence.** EXEC_BUFFER_VALUE, EXEC_MAX_SLIPPAGE_POINTS, EXEC_MIN_TP_GAP_POINTS, RISK_SL_SLIPPAGE_ALLOWANCE_POINTS, RISK_MAX_SL_DISTANCE and RISK_FIXED_SIZE are single numbers in absolute price or size units. Values tuned for gold (buffer 2.0, slippage 3.0, maximum stop distance 30) mean something entirely different on BTC at 82,000, ETH at 2,500 and SOL at 109, and one fixed size cannot fit three symbols (0.004 is 330 USDC of BTC but 0.44 USDC of SOL, under the 10 USDC minimum). The harness had to start the bot once per symbol with rescaled settings.

**Impact.** The bot cannot trade several symbols correctly from one configuration. With HL_SYMBOL_MAP holding more than one symbol, some signals are rejected by risk rules and others are sized or buffered wrongly.

**Reproduce.** Set HL_SYMBOL_MAP to BTC and SOL with RISK_SIZING_MODE=fixed and RISK_FIXED_SIZE=0.004, then inject one signal for each.

**Recommended fix.** Express these settings as a percentage or basis points of price, or allow per-symbol overrides (for example a JSON map per setting); size by notional or by risk rather than by a single quantity.

### D4 (Low): The first request after an idle period fails with a 5 s read timeout, and read-only queries are classified 'unknown outcome'

**Evidence.** After about four minutes without a request the next REST call hung until the 5 s timeout: 10:00:15 and 10:04:53 in the official run, 10:25:57 and 10:28:00 in the retry (the Mac was kept awake for the retry, so this is not the sleep problem). The second attempt succeeded at once. The adapter raises OutcomeUnknown for every timeout, including /info reads that cannot change anything.

**Impact.** For the running bot the effect is small: it queries every few seconds, and a lost answer to a signed request is resolved correctly by client id (seen in TC17). It costs up to 5 s when it happens on a stop-loss placement. For any tool built on the adapter that idles, the first read fails. This is what turned TC17 and TC18 into failures in the official run.

**Reproduce.** Create the adapter, wait 240 s, call get_account().

**Recommended fix.** Retry idempotent /info reads once on a fresh connection; classify a read timeout as transient rather than unknown outcome; consider a shorter connect timeout than read timeout.

### D5 (Low): The exchange accepts a second order with a client order id that is already in use, and the first order then cannot be cancelled by that id

**Evidence.** Two resting ETH buys were accepted under the same client id (order ids 62412703046 and 62412703581). cancelByCloid removed the newer one; a second cancelByCloid answered 'Order was never placed, already canceled, or filled' while 62412703046 was still open. It had to be cancelled by exchange order id. Same result in the retry.

**Impact.** The bot derives a unique client id for every order and never reuses one, so this cannot happen in normal operation. If an id were ever reused, the bot, which cancels only by client id, could not remove the older order. This settles open item IV-06 in docs/INTEGRATION_VERIFICATION.md: duplicates are not rejected.

**Reproduce.** Place a resting order, place a second with the same cloid, cancel by cloid twice, list open orders.

**Recommended fix.** Keep relying on unique ids (already enforced by the database). Add a cancel-by-order-id fallback in the reconciler for an order that the exchange still lists after a cancel by client id reported 'not found', and record the result under IV-06.

### D6 (Low): ExchangeError cannot be copied or pickled

**Evidence.** copy.deepcopy(ExchangeError(kind, text)) raises AttributeError: 'str' object has no attribute 'value', because the exception passes one formatted string to Exception.__init__ and is rebuilt from it. dataclasses.asdict() on a PlaceResult that carries an error fails the same way. In the official run this stopped the harness from recording the exchange's rejection texts for subtests a-f and h (the orders were rejected; the reasons were captured in the retry).

**Impact.** None in the bot today (it never copies these objects). It will bite any code that logs, copies or passes such results between processes.

**Reproduce.** python -c "import copy; from hlbot.exchange.base import ExchangeError, ErrorClass; copy.deepcopy(ExchangeError(list(ErrorClass)[0], 'x'))"

**Recommended fix.** Call super().__init__(kind, text) and build the message in __str__, or define __reduce__ to return (type(self), (self.kind, self.text)).

## Side effects on your account and bot

- Your bot was stopped twice, for about 13 and 9 minutes, and restarted with your own `.env`. It is running again (testnet, HEALTHY).
- The test bot used its own database, so your dashboard's trade history and win statistics contain no test trades. **Day P/L does include the test fees**: the bot counts every fill on the account for the day's result and for the daily-loss limit, whoever placed the order. The tests cost about 0.54 USDC, 0.27 % of the account, against a 2 % limit.
- ETH and SOL leverage on the account is now 5x cross (the bot sets leverage before an entry). BTC was already 5x.

## Not covered

- A stop-loss actually triggering under test control (the harness cannot move the market). Your own BTC trade earlier today shows one: stop FILLED, take-profits cancelled.
- A partially filled IOC entry, liquidation, funding payments, isolated margin, HIP-3 markets, and anything on mainnet.
- Multi-take-profit partial closure on BTC under clean conditions: observed in the official TC17 only through the bot's own records, blocked by a quiet market in the retry.

## Deliverables

| What | Where |
|---|---|
| Test runner (all 20 cases, safety checks, cleanup) | `qa/run_qa.py` |
| Test configuration | `qa/qa_config.json` |
| This report generator | `qa/make_report.py` |
| Report, Markdown and JSON | `qa/results/run-20261011/report.md`, `qa/results/run-20261011/report.json` |
| Raw results: every step, request, response, latency and assertion | `qa/results/run-20261011/results.json` |
| Execution log | `qa/results/run-20261011/execution.log` |
| Defects | `qa/results/run-20261011/defects.json` |
| Test bot evidence: database with the full audit trail, logs, the settings used per symbol | `qa/results/run-20261011/bot/`, `qa/results/run-20261011/bot-*.env` |
| Retry run (TC17, TC18, TC20) | `qa/results/retry-20261011/` |

Re-run: stop the bot, then `.venv/bin/python qa/run_qa.py --preflight` (checks only) and `caffeinate -i .venv/bin/python qa/run_qa.py`.
