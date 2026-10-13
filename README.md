# hlbot — Telegram signal trading bot for Hyperliquid

Listens to Telegram signal channels, parses gold signals of the form
*"SELL, zone 4286–4289, SL 4296, TP1…TP5"*, and — while the live price is
inside the signal zone plus a buffer — opens **one position on Hyperliquid
with one stop-loss covering all of it and a ladder of reduce-only
take-profits**. It is the rebuild specified in [PRD.md](PRD.md) of an
earlier MetaTrader 5 bot.

The design puts capital protection ahead of features:

- every order has a persisted intent before it is sent;
- every exchange response is treated as provisional and confirmed against
  exchange state;
- a timed-out request is resolved by lookup before anything is retried;
- "position open" and "position protected" are separate, verified states,
  with a hard deadline after which an unprotected position is flattened;
- trading pauses by itself whenever account or market state cannot be
  verified.

> **Status.** The logic is complete and tested against a simulated
> exchange, and the read path and paper mode have been run against live
> Hyperliquid market data. **Nothing has been run with a funded account,
> on testnet or mainnet.** Fourteen exchange behaviours the design depends
> on are still unproven — see
> [docs/INTEGRATION_VERIFICATION.md](docs/INTEGRATION_VERIFICATION.md).
> Do not trade real funds until those are closed on testnet.
> [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) lists what is verified
> and how.

## Modes

| Mode | What happens | Needs |
|---|---|---|
| `paper` (default) | Fills are simulated against live **mainnet** market data. Every validation and risk check runs. No signing key is ever loaded. | Nothing |
| `testnet` | Real API, mock funds. Same code path. | Account address, agent key file |
| `mainnet` | **Real funds.** | `HL_ENV=mainnet` **and** `--mainnet` on the command line **and** `HL_MAINNET_CONFIRM` equal to the account address |

If any of the three mainnet conditions is missing, the bot runs in paper
mode and says so at startup, in `botctl status`, and on the dashboard.
Every record and notification is labelled with its mode.

Simulated results are not execution results: the simulator has no queue
position, no book depth and no funding, and always fills at the touch.

## Quick start (paper mode, no credentials)

Requires Python 3.11, 3.12 or 3.13 on Linux or macOS.

```bash
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --no-deps -e .

cp .env.example .env
# For a first look without Telegram, set in .env:  TELEGRAM_ENABLED=false

.venv/bin/botctl config          # resolved settings, secrets hidden
.venv/bin/botctl start           # background; logs/bot.out, logs/hlbot.log
.venv/bin/botctl status
.venv/bin/botctl price
```

The dashboard is at <http://127.0.0.1:8787/> and needs a sign-in. The
first visit offers **first-time setup**: choose a username and password
for the first admin and paste the control token (the contents of
`data/control.token`, which proves access to the bot host). After that
there are three roles:

| Role | May |
|---|---|
| `viewer` | read everything the dashboard shows |
| `operator` | also pause, resume, kill, close and the other commands |
| `admin` | also change the dashboard name and logo, and manage accounts |

In **History**, a trade row opens to show its orders: the entry, the
stop-loss, every take-profit level and the close, with what filled and
at what price.

**Market** shows, per symbol, the volume of the candle now forming, the
average of the last 20 closed candles, how the last closed candle
compares with that average (and whether it is unusually high or low),
and the rolling 24-hour volume. Base volume is in the coin and exact;
quote volume is exact for 24 hours and an estimate (marked ≈) for a
candle; the trade count is the tick volume. The chips choose the candle
size (15m, 1H, 4H, 1D).

**Analytics** computes its figures from the bot's own records: total
trades, win rate, net P/L, gross profit and loss, profit factor, average
win and loss, average planned reward to risk, maximum drawdown, fees and
funding, and the account's equity; with charts of the cumulative net
P/L, equity and realised balance, P/L and activity by day, week or
month, the distribution of results, and performance by symbol, signal
source and direction. Filters: date range, symbol, signal source and
direction. The rules it follows:

- one trade counts once, however many orders and fills it had; its
  result is `realised - fees + funding` as recorded from the exchange's
  fills;
- only closed trades count; a win has a net result above zero, a loss
  below zero;
- a trade belongs to the moment it closed, and days follow the viewer's
  time zone;
- the equity and balance lines are for the whole account, so only the
  date range applies to them.

**History** has the same filters plus status, pages of 20, and
**Export to Excel**: a `trade-history-YYYY-MM-DD.xlsx` with every trade
the filters select (not just the page on screen) on a *Trades* sheet,
every execution on an *Executions* sheet, and a *Summary* sheet with the
filter and the key figures. Any signed-in role may export; each export
is written to the audit trail.

### Scenario outlook

**Scenario outlook** on the dashboard is a short brief for each session
(Asia, London, New York; Monday to Friday): context, key levels and
three if/then scenarios (rejection, breakout, breakdown), each with a
trigger, targets and an invalidation. It is a **scenario outlook, not
news and not a signal**: it sends no order, the trading engine never
reads it, and a failure in it cannot stop the bot. The full
specification is [docs/outlook.md](docs/outlook.md).

- **Validity.** Every brief shows when it was generated, how long it is
  valid for and a status: *Fresh*, *Aging* (past 75 % of its validity)
  or *Stale*. A stale brief is dimmed and labelled. All times are shown
  in the outlook's zone (`OUTLOOK_TZ_NAME`, WIB by default). A
  high-impact release (CPI, PPI, the jobs report, FOMC, a speech by the
  Fed Chair) makes the running brief stale and a refreshed one follows a
  few minutes later; if both major levels break, the brief is marked
  stale. An operator can ask for a fresh brief (**Refresh outlook**,
  once every five minutes).
- **Sources.** Every level and claim names its sources. Levels from
  different sources within `OUTLOOK_CONFLUENCE_TOLERANCE` are one level;
  two or more sources make it a **confluence**, one a **single source**.
  Sources that lean opposite ways give a **mixed** bias with both views
  listed. An event's time is confirmed only when two sources agree. A
  source that does not answer is listed as unavailable and the brief is
  built from the rest ("5 of 6 sources available"); below
  `OUTLOOK_MIN_LEVEL_SOURCES` it is marked **low confidence**. What the
  sources did not supply is shown as *Unavailable*, never filled in.
- **Price.** The price, the distance to each level and the nearest level
  come from the same live quote as the market card. There is no second
  feed.
- **My Version.** Each user can add their own levels and edit a
  scenario's trigger, entry zone, stop, targets and invalidation. Edits
  pass the same checks as a generated scenario (stop and targets on the
  right side of the entry, an invalidation present); a refused edit
  shows the fault beside the field and saves nothing. The generated
  brief is never changed, and **Reset** goes back to it. A user's levels
  carry over to the next brief while they are within
  `OUTLOOK_CARRY_DISTANCE` of price; others are archived.
- **History.** Past briefs with each scenario's outcome (*not
  triggered*, *triggered, hit target*, *triggered, hit invalidation*,
  *triggered, expired open*), read from the 5-minute candles, with
  counts by session, scenario and week and a private journal note per
  brief. Edited scenarios are counted apart. Past outcomes do not
  predict future results.

The source registry is configuration. On by default: four technical
readings computed from Hyperliquid's candles for the managed gold market
(daily pivots, previous day and week range, 4H/1H swing points, 1H/4H
moving averages) and two official calendars (BLS release schedule,
Federal Reserve calendar). Built in but **off until you have checked
their terms of use**: a calendar aggregator (`ff-calendar`) and a news
feed (`investing-commodities`, headlines and links only). With only the
official calendars, event times show as *Time unconfirmed* (each event
has one source) and the drivers come from the calendars alone.

```
OUTLOOK_SOURCES_ENABLE=ff-calendar,investing-commodities
OUTLOOK_SOURCES_FILE=~/.config/hlbot/outlook-sources.json   # change or add sources: [{"id": ..., "kind": ..., "url": ...}]
```

Session times are configuration too (`OUTLOOK_SESSIONS`): London and New
York move with daylight saving, so adjust them twice a year. Every
setting is in [.env.example](.env.example). The levels are those of the
Hyperliquid market the bot trades, which can sit a few points from spot
XAU/USD.

Accounts are managed in **Settings → Users**, or from the bot host:

```bash
.venv/bin/botctl user add alice --role operator   # asks for the password
.venv/bin/botctl user list
.venv/bin/botctl user passwd alice                # also the way back in after a forgotten password
.venv/bin/botctl user role alice --role viewer
.venv/bin/botctl user disable alice               # or: enable, remove
```

Passwords are stored as salted scrypt hashes and sessions as hashes of a
random token, in the bot's database. A session lasts 12 hours; five wrong
passwords lock a name for 15 minutes. The sun/moon button switches
between the dark and light themes (remembered per browser). **Settings →
Appearance** changes the dashboard name and the logo (PNG, JPEG, WebP or
GIF up to 512 KB, stored as `data/ui-logo`).

To rehearse without waiting
for a channel to post, feed a message by hand (never available on
mainnet):

```bash
.venv/bin/botctl inject "#GOLD SELL NOW\nGold sell Zone 4190- 4193\n\nSL : 4200\n\nTP1 : 4187\nTP2 : 4184\nTP3 : 4181\nTP4 : 4178\nTP5 : 4174"
.venv/bin/botctl pending         # waiting for price, with the reason if not entered
.venv/bin/botctl positions
.venv/bin/botctl orders
.venv/bin/botctl kill            # close everything reduce-only and pause
.venv/bin/botctl resume
.venv/bin/botctl stop
```

## Configuration

Non-secret settings live in `.env` (every variable is documented in
[.env.example](.env.example)). A malformed value stops startup with a
message naming the variable. Secrets never go in `.env`; the bot refuses
to start if it finds one there.

| Secret | Where it lives |
|---|---|
| Telegram API hash, notification bot token | Owner-only file named by `SECRETS_FILE` (create with `botctl init-secrets PATH`), or the process environment when a secret manager injects it |
| Hyperliquid **agent** (API wallet) key | Alone in an owner-only file named by `HL_AGENT_KEY_FILE`. Never the master key. Never read in paper mode |
| Telethon session | `TELEGRAM_SESSION`, default `~/.config/hlbot/telegram.session`. A full-account credential: outside the repository, mode `0600` |

A secret file readable by group or others (anything but `0600`) is
refused at startup.

### Telegram

```bash
botctl init-secrets ~/.config/hlbot/hlbot.secrets   # then fill in TELEGRAM_API_HASH (and the bot token)
# .env: TELEGRAM_API_ID, TELEGRAM_CHANNEL, SECRETS_FILE
botctl telegram-login                               # one interactive login; creates the session file
```

Prefer a dedicated Telegram account for scraping. Channels are given as
`@username`, an invite link, or a numeric id, comma-separated. A channel
that uses the "ENTRY … Limit …" format is mapped in configuration:
`TELEGRAM_CHANNEL_PARSERS={"-1001234567890": "entry_limit"}`.

Notifications go through a separate bot (`TELEGRAM_NOTIFY_ENABLED=true`,
`TELEGRAM_NOTIFY_CHAT_ID`, token in the secrets file). They are written to
a persistent outbox and retried for 24 hours, so none is lost across a
restart or a Bot API outage.

### Hyperliquid (testnet first)

1. Use a wallet dedicated to the bot, holding only the capital meant for
   the strategy.
2. In the Hyperliquid UI, create an **API (agent) wallet** for that
   account. Save its private key alone in a file, `chmod 600`.
3. Set `HL_ENV=testnet`, `HL_ACCOUNT_ADDRESS` (the **account**, not the
   agent) and `HL_AGENT_KEY_FILE`.
4. `botctl start`. Startup fails closed if the account cannot be read or
   the key is not an authorised agent of that account. The master key is
   refused outright.

Use a different key for testnet and mainnet, and never reuse an agent
address after it expires or is removed: generate a new key each time.

### Risk controls

Every control is on by default. A failed check means **no order at all**;
the size is never reduced to make a trade fit.

| Setting | Default | Meaning |
|---|---|---|
| `RISK_PER_TRADE_PCT` | 0.5 | Loss at the stop, fees and a slippage allowance included, as % of equity. Sets the size |
| `RISK_MAX_DAILY_LOSS_PCT` | 2.0 | No new entries once the UTC day's realised + unrealised P/L reaches this loss |
| `RISK_MAX_OPEN_TRADES` | 3 | Across all assets (one per asset, fixed) |
| `RISK_MAX_EXPOSURE_X_EQUITY` | 3.0 | Total notional after entry |
| `RISK_LEVERAGE` | 5 | Set on the exchange and verified before the first entry on an asset |
| `RISK_MIN_RR` | 0.5 | Size-weighted reward-to-risk at the entry limit price |
| `RISK_MAX_SL_DISTANCE` | 30 | Price points between entry limit and stop |
| `RISK_MARGIN_USAGE_CAP` | 0.5 | Share of available margin one trade may use |
| `RISK_LIQ_BUFFER_X` | 1.5 | Estimated liquidation must be this many stop-distances away |
| `RISK_MAX_TRADE_NOTIONAL` | 5000 | USDC per trade |

`RISK_MAX_TRADE_NOTIONAL` interacts with account size: at 0.5 % risk and a
12-point stop, an account above roughly 2,400 USDC sizes trades over the
5,000 USDC cap, and those trades are rejected. Raise the cap or lower the
risk percentage deliberately.

## How a trade runs

1. A message arrives and is stored verbatim, keyed by `(channel, message)`.
2. It is classified, parsed and validated. An invalid signal is stored
   with its reasons and never traded; you get a notification either way.
3. While it waits, every best-bid/offer update is checked against the
   buffered zone. No decision is made on a quote older than 2 s. On a
   quiet market `bbo` pushes nothing for many seconds, so the bot
   re-confirms the top of book over REST before deciding.
4. In range: fresh account state is read, the trade is sized, and every
   risk control runs.
5. The entry is a **limit IOC** at the touch ∓ `EXEC_MAX_SLIPPAGE_POINTS`,
   clamped so a fill can never land beyond TP1, with a client order id and
   a 5-second deadline.
6. The fill is confirmed against order status and fills.
7. A reduce-only stop-market for the whole position goes out, then the
   take-profit ladder. The trade is `PROTECTED` only once the exchange's
   own open-order list shows the stop. Not there after 3 s: re-placed
   once. Not there after 8 s: the position is flattened and the asset is
   paused until you resume it.
8. After each take-profit the stop is resized to what remains. A stop
   cancelled outside the bot is re-placed immediately, even while paused.
9. At zero position, leftovers are cancelled and the trade is classified
   from the fills (`WIN_ALL`, `WIN_PARTIAL`, `LOSS_ALL`, `MANUAL`,
   `FLATTENED`, `LIQUIDATED`) with P/L = realised − fees + funding.

**Differences from the MT5 bot that matter to you**

- One position per asset, not one per take-profit. A new signal on an
  asset with an open trade is rejected (`POSITION_OPEN`).
- Stops and trigger orders fire on Hyperliquid's **mark price**, not the
  bid/ask of a broker feed, and a stop-market can fill up to 10 % from its
  trigger in a thin book.
- Signal levels were written against a broker's gold feed. The
  Hyperliquid gold markets sit a few points away from it and from each
  other. `EXEC_PRICE_OFFSET_POINTS` and `RISK_MAX_MARK_ORACLE_DEV_POINTS`
  exist for this; measure the offset in paper mode before trusting it.
- Sizes are units of the underlying, not lots.
- Retired settings (`MT5_*`, `EXEC_ORDER_TYPE`, `EXEC_SLIPPAGE_ACTION`, …)
  are ignored with a warning.

## Operating it

| Command | Effect |
|---|---|
| `botctl start [--mainnet]` / `stop` / `restart` | Background process. `stop` is graceful: exchange-resident SL/TP orders stay in place |
| `botctl status` / `positions` / `orders [N]` / `pending` / `price` / `logs [N] [-f]` | Read-only. Fall back to the database when the bot is down |
| `botctl pause` / `resume` | Stop / allow new entries. Persisted across restarts; works while the bot is down |
| `botctl kill` | Pause, close every bot position reduce-only, cancel bot orders, verify flat. Never touches a position or order the bot did not create |
| `botctl close TRADE` | Close one trade reduce-only |
| `botctl reconcile [--adopt TRADE \| --close TRADE]` | Run a reconciliation pass; resolve a trade whose position was changed outside the bot |
| `botctl modify-sl TRADE PRICE` | Move a stop through the exchange modify action |
| `botctl ack` | Stop the repeating `HALTED` alert without resuming |
| `botctl trace --fill N \| --order N \| --trade N \| --signal N` | The whole chain: message → signal → risk assessment → orders → fills, with audit events |
| `botctl backup [DEST]` | Consistent database backup |
| `botctl config` | Resolved settings, secrets hidden |
| `botctl test` | Run the test suite |

**Health states.** `HEALTHY`; `DEGRADED` (entries blocked on an asset or
by a condition that clears itself: stale data, daily loss, foreign
position, rate limit); `PAUSED` (operator pause, or clock drift, an
unresolved order, an expiring key…); `HALTED` (flatten not verified, or a
liquidation: manual `resume` only, alert repeats every 60 s until `ack`).
Cancels, reduce-only closes and stop-loss placement are never blocked by
any of these.

A position or order on a managed asset that the bot did not create blocks
entries on that asset and raises an alert. It is never modified. Give the
bot its own wallet.

Day-to-day procedures — key rotation, backup and restore, deploy and
rollback, what each alert means — are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Deployment

Run under a supervisor so a crash is followed by a restart and a clean
recovery; open positions stay protected throughout because the protective
orders live on the exchange.

- **systemd:** [deploy/hlbot.service](deploy/hlbot.service) (restart after
  5 s; exit codes 2 "bad configuration" and 3 "already running" do not
  restart).
- **Docker:** `docker compose up -d --build` with
  [docker-compose.yml](docker-compose.yml). Secrets and the Telethon
  session are mounted, never baked into the image. Do not scale the
  service: one process per account.

The host needs NTP: entries pause above 2 s of clock drift. The local API
binds to `127.0.0.1`; for remote access use an SSH tunnel or an
authenticating reverse proxy, and serve it over HTTPS there.
When a proxy or tunnel publishes the dashboard, tell the bot the public
address, or signing in from it is refused ("... is not an address this
bot is published at"):

```
API_PUBLIC_ORIGINS=https://dashboard.example.com
```

The bot then accepts sign-ins from that address, marks the cookie
`Secure`, and counts failed sign-ins per visitor (`CF-Connecting-IP` /
`X-Forwarded-For`) instead of per proxy. A published dashboard is
reachable by anyone who has the address: put an access policy in front
of it (for example Cloudflare Access) and give `operator` and `admin`
only to people who may trade.

`GET /health` needs no credential. `GET /metrics` (Prometheus text) and
every `/api/` route need a signed-in account or the control token
(`Authorization: Bearer <contents of data/control.token>`; the token is
new on every start). `data/heartbeat` is
rewritten every second for an external liveness check.

Never deploy or restart on purpose while a trade is `OPEN_UNPROTECTED` or
`FLATTENING`.

## Development

```bash
.venv/bin/pip install --require-hashes -r requirements-dev.lock && .venv/bin/pip install --no-deps -e .
.venv/bin/ruff check hlbot tests
.venv/bin/mypy
.venv/bin/pytest --cov=hlbot          # about 100 s
```

After changing a dependency in `pyproject.toml`, install it and run
`.venv/bin/python scripts/make_lock.py` to rewrite both lockfiles with the
sha256 digests PyPI publishes; review the diff before committing.

```
hlbot/
  domain/     pure logic: parser, validator, planning, rounding, risk, state machines, scenario outlook rules
  exchange/   adapter interface, Hyperliquid adapter (the only SDK import), simulator, paper adapter
  core/       engine, signal manager, reconciler, health, action queue, notifier, scenario outlook service, app wiring
  db/         SQLite (WAL, synchronous=FULL), forward-only migrations, append-only audit log
  ingest/     Telegram listener (Telethon), scenario outlook source readers
  api/        local read API, metrics, dashboard
  cli.py      botctl
tests/
  unit/       pure logic, adapter parsing and signing, architecture rules
  component/  the whole application against a scriptable simulated exchange
```

Two rules are enforced by tests: only `hlbot/exchange/hyperliquid.py`
imports the exchange SDK, and no withdrawal, transfer, agent-approval or
sub-account-funding call exists anywhere in the code.

The baseline's 393 tests are ported or retired with a written mapping in
[docs/TEST_PORT_MAPPING.md](docs/TEST_PORT_MAPPING.md).

## Disclaimer

This software places leveraged orders with real money when configured for
mainnet. Nothing in it guarantees a fill price, zero slippage, or exchange
availability. Whether you may use Hyperliquid, and automate trading on it,
where you live is yours to confirm.
