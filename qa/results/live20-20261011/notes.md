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
