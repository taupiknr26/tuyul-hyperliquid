# Runbook

For the operator. Commands assume `botctl` is on the path and run from
the directory holding `.env` (or pass `--env-file`).

The drills the PRD asks for before handover (gate G5: restore, rollback,
alert test) have **not** been performed. The procedures below are written
from the code and exercised by the automated tests and one paper-mode
run; rehearse each once on testnet before relying on it.

## Every day

```bash
botctl status        # process, mode, health, active conditions
botctl positions     # exchange positions and bot trades
botctl pending       # signals waiting for price, with the reason they are waiting
botctl logs 50 -f
```

The dashboard at `http://127.0.0.1:8787/` shows the same, refreshed every
3 s, with a "data is stale" marker if it stops updating.

## Start, stop, restart

```bash
botctl start              # paper or testnet, from HL_ENV
botctl start --mainnet    # also needs HL_ENV=mainnet and HL_MAINNET_CONFIRM
botctl stop               # graceful
```

`stop` leaves stop-loss and take-profit orders on the exchange; they keep
protecting any open position while the bot is down. On the next start the
bot reads its unfinished work, the exchange's orders, positions and
fills, resolves anything unknown, re-verifies protection, and only then
allows entries.

Do not restart on purpose while `botctl positions` shows a trade in
`OPEN_UNPROTECTED` or `FLATTENING`.

A second instance against the same account refuses to start (exit
code 3). Never work around this with a second data directory.

## Pause and kill

| | New entries | Open trades |
|---|---|---|
| `botctl pause` | Stopped until `resume`, across restarts | Managed normally |
| `botctl kill` | Stopped until `resume` | Closed at market, reduce-only, bounded at 1 %–5 % from mark; bot orders cancelled; verified flat |

`kill` reports what remains if the exchange refuses a close, alerts, and
keeps retrying. It never touches a position or order the bot did not
create.

## Alerts and what to do

| Alert | Meaning | Action |
|---|---|---|
| `PAUSED [coin]` "could not be protected … flattening" | The stop was not visible on the exchange within 8 s. The position was closed | Check the trade with `botctl trace --trade N`. `botctl resume` when satisfied |
| `HALTED` "could not be flattened" (repeats every 60 s) | The bot could not close a position it could not protect. **The position may be unprotected** | Close it by hand on the exchange now. Then `botctl reconcile`, `botctl ack`, and `botctl resume` |
| `HALTED` "liquidation" | The exchange liquidated a position | Review; `botctl resume` when ready |
| "Stop-loss missing or under-sized" | A stop was cancelled or shrunk outside the bot. It is being re-placed | Find out what cancelled it |
| "Entries blocked [coin] … not created by the bot" | A position or order on a managed asset that the bot does not own | Close or cancel it yourself. The block clears by itself. Give the bot its own wallet |
| "position changed by an order the bot did not place" | A manual order changed a bot trade's size. The stop now covers the actual size | `botctl reconcile --adopt N` to accept the new size, or `--close N` |
| "entry … position present but order and fills not found" | An entry timed out and the evidence is contradictory | Look at the exchange. If the bot does not own the position: `botctl reconcile --close N` (the position is then treated as foreign) |
| "Agent key expires soon" / `PAUSED` "agent key" | 14 days / 24 hours before expiry, or the exchange rejected the signer | Rotate the key (below), then `botctl resume` |
| "Entries blocked — daily loss limit reached" | The day's loss reached `RISK_MAX_DAILY_LOSS_PCT` | None. Clears at 00:00 UTC |
| `PAUSED` "clock" | Local clock is more than 2 s from exchange time | Fix NTP. Clears by itself |
| "Market data stream down for 30 s" | WebSocket down. Entries blocked; open trades managed over REST | Check connectivity. Clears by itself |
| "Rate-limit headroom low" | Under 20 % of the address-based budget left | Trade volume restores it. Protective actions are prioritised if it runs out |
| "Signal message #N was edited / deleted" | The channel changed a message that produced a signal | Decide by hand. The bot changes nothing |
| "Stop-loss slippage exceeded allowance" | A stop filled further from its trigger than `RISK_SL_SLIPPAGE_ALLOWANCE_POINTS` | Consider a larger allowance, lower leverage, or a deeper market |
| "DEFECT: order failed exchange validation" | The exchange rejected a tick or minimum value the bot should have prevented | Report it with `botctl trace` output; the signal was not traded |

## Dashboard accounts

The dashboard needs a sign-in; `botctl` on the bot host does not (it uses
`data/control.token`).

| Situation | Do |
|---|---|
| No account yet | Open the dashboard: it offers first-time setup, which asks for the control token. Or `botctl user add NAME --role admin` |
| Someone forgot a password | `botctl user passwd NAME` (signs that account out everywhere) |
| The only admin is locked out or gone | `botctl user add NEWNAME --role admin`, then remove the old one |
| Someone should lose access now | `botctl user disable NAME` (ends their sessions at once) |
| "too many failed sign-ins" | Wait 15 minutes, or restart the bot: the counter is in memory |

Give `operator` only to people who may press Kill. A `viewer` sees
positions and P/L but cannot send any command.

## Rotate the agent key

1. `botctl pause`. Wait until `botctl orders` shows nothing in `INTENT`,
   `SUBMITTING` or `UNKNOWN`.
2. In the Hyperliquid UI create a **new** API wallet. Never reuse an old
   address: once an agent is removed or expires its nonce history can be
   pruned and old signed actions replayed.
3. Write the new private key alone to a new file, `chmod 600`.
4. Point `HL_AGENT_KEY_FILE` at it. `botctl restart`.
5. Startup verifies the new key is an agent of the account. `botctl resume`.
6. Remove the old agent in the UI and delete the old key file.

Open positions stay protected throughout: protective orders live on the
exchange and do not depend on the key.

## Backup and restore

```bash
botctl backup                       # data/backups/hlbot-<env>-<UTC time>.sqlite3
```

Back up daily and before every deploy (for example from cron). The file
is a consistent snapshot taken while the bot runs.

Restore:

1. `botctl stop`.
2. Move the damaged database (and its `-wal` / `-shm` files) aside; do
   not delete them.
3. Copy the backup to `data/hlbot-<env>.sqlite3`.
4. `botctl start`. Startup recovery fetches fills since the last stored
   fill and reconciles against the exchange. Trades opened after the
   backup are not in it: their positions and orders are flagged as
   foreign, entries on that asset are blocked, and you close or keep them
   by hand.

A database that fails its integrity check stops startup. The bot never
starts from an empty state on its own.

## Deploy and roll back

1. `botctl pause`; wait for no order in `INTENT` / `SUBMITTING` /
   `UNKNOWN` and no trade in `OPEN_UNPROTECTED` / `FLATTENING`.
2. `botctl backup`.
3. `botctl stop`; install the new version from its lockfile; `botctl start`
   (migrations run at startup and are forward-only).
4. Check `botctl status`, then `botctl resume`.

Rollback is the same sequence with the previous version. A migration must
keep the previous release working for one version; if a release note says
otherwise, restore the pre-deploy backup instead.

## Going to mainnet

The PRD's gates, in order. None has been passed.

1. **G2 testnet:** close every item in
   [INTEGRATION_VERIFICATION.md](INTEGRATION_VERIFICATION.md); ≥ 50
   testnet trades with p95 time-to-protected ≤ 3 s; forced kills.
2. **G3 paper soak:** ≥ 10 trading days on live signals with no unhandled
   exception and no unresolved divergence. Use it to measure the offset
   between the signal's levels and the Hyperliquid market.
3. **G4 canary:** ≥ 20 mainnet trades at minimum size with tight caps
   (`RISK_MAX_TRADE_NOTIONAL`, `RISK_PER_TRADE_PCT`,
   `RISK_MAX_DAILY_LOSS_PCT`), P/L matching the exchange to the cent.
4. Raise caps only by deliberate, separate decisions.

Before the first mainnet start: a dedicated wallet holding only trading
capital, an agent key that is not the testnet one, secrets files at
`0600`, the old MT5 password / API hash / bot token rotated if the old
`.env` was ever exposed, and an external check on `GET /health` or the
age of `data/heartbeat`.
