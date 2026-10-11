# Implementation plan and verification status

Source of truth: [PRD.md](PRD.md) (v1.0, 2026-10-10). Last updated 2026-10-11.

## Where this stands

The application described by the PRD is implemented: 40 source modules
(about 10,000 lines) and 536 automated tests. Every PRD requirement that
can be proven without a Hyperliquid account or Telegram credentials has
been implemented and verified. **The requirements whose acceptance
criteria name testnet, mainnet or a multi-day soak are blocked on those
credentials and are marked as such below.** Nothing has been run with a
funded account on either network.

PRD acceptance gates (§16.3):

| Gate | Status | |
|---|---|---|
| G1 — Logic complete | `[x]` | All unit and component tests pass; coverage per NFR-017; baseline tests ported or retired with a mapping |
| G2 — Testnet proven | `[-]` | Started 2026-10-10: one PAXG trade entered, protected and matched against the exchange. Parts of IV-04, 08, 10, 11 and IV-12 observed; none of the 14 fully closed; no stop trigger, modify, cancel or forced-kill run yet ([docs/INTEGRATION_VERIFICATION.md](docs/INTEGRATION_VERIFICATION.md)) |
| G3 — Paper soak (≥ 10 trading days on live signals) | `[!]` | Needs Telegram credentials and elapsed time. Paper mode itself runs against live market data |
| G4 — Canary (≥ 20 mainnet trades) | `[!]` | After G2 and G3; owner decision |
| G5 — Handover (restore, rollback, alert drills) | `[!]` | Runbook written; drills not performed |

### Status legend

- `[ ]` Not started
- `[-]` In progress: implemented, with part of its acceptance criterion not yet measured
- `[x]` Implemented and verified by the checks listed
- `[!]` Blocked: implemented and verified as far as possible here; the rest needs something this environment does not have

### Verification levels

| Code | Meaning |
|---|---|
| **U** | Unit test: pure logic, no network |
| **C** | Component test: the whole application (engine, reconciler, signal manager, health, queue, notifier, database) against the scriptable simulated exchange, or the adapter against stubbed HTTP / a local WebSocket server |
| **L** | Run against live Hyperliquid **mainnet public market data**, read-only or in paper mode (2026-10-11) |
| **T** | Hyperliquid testnet with an account — **not available; nothing has this level** |

## Initial assessment

- **Repository at the start:** `PRD.md` only. No code, no git repository.
  The baseline application the PRD analyses is in the sibling directory
  `../telegram-scraper` and was used read-only as the source for ported
  logic and tests. It was not modified.
- **Stack (PRD §11.3):** Python, asyncio, SQLite (WAL), the official
  `hyperliquid-python-sdk` 0.24.0 behind one adapter, Telethon, aiohttp for
  the local API and the WebSocket client. Declared `>=3.11,<3.14`; the
  full suite passes on CPython 3.11.9 and 3.13.9 (macOS arm64) and on
  3.12.14 (Linux aarch64, in a container).
- **Not available here:** a Hyperliquid account or agent key (testnet or
  mainnet), Telegram API credentials or a session, a notification bot
  token, a Linux host.

### Decisions taken on the PRD's open questions

The owner has not answered §19. The PRD's own recommendation was adopted
for each, and each is a configuration default that can be changed.

| OQ | Decision | Where |
|---|---|---|
| OQ-01 instrument | `GOLD → xyz:GOLD`, configurable | `HL_SYMBOL_MAP` |
| OQ-02 price offset | Levels as-is; optional fixed offset and mark/oracle guard, both off | `EXEC_PRICE_OFFSET_POINTS`, `RISK_MAX_MARK_ORACLE_DEV_POINTS` |
| OQ-03 account structure | Dedicated wallet assumed; sub-account supported but untested | `HL_VAULT_ADDRESS` |
| OQ-04 signal while a trade is open | Rejected (`POSITION_OPEN`) | fixed at one trade per asset |
| OQ-06 TP style | Resting reduce-only limit; trigger style selectable | `HL_TP_STYLE` |
| OQ-07, OQ-08 buffer, slippage, R:R, risk defaults | The values in PRD §10.6 | `.env.example` |
| OQ-09 partial fill | Accept | `EXEC_PARTIAL_FILL_POLICY` |
| OQ-10 break-even after TP1 | Implemented, off | `EXEC_SL_BREAKEVEN_AFTER_TP1` |
| OQ-11 pause/kill through the Telegram bot | Not built (PRD §10.8: local CLI in v1) | — |
| OQ-12 dashboard | Built, minimal, read-only plus pause/resume/kill | `hlbot/api/` |
| OQ-18 day boundary | UTC | — |
| OQ-05, 13–17 | Owner matters; nothing to implement | — |

### Engineering decisions and assumptions worth knowing

1. **The SDK's `Exchange` class is not used.** It contains withdrawal and
   transfer methods and draws its own nonces. The adapter uses the SDK's
   signing functions directly and builds only `order`, `batchModify`,
   `cancelByCloid` and `updateLeverage`, with nonces from the action queue.
2. **The stop-loss is a fixed-size reduce-only trigger resized by `modify`
   after each TP fill** (the PRD's IV-03 fallback), not `positionTpsl`,
   whose semantics are unproven.
3. **Risk rejections are terminal** (`REJECTED_RISK`), following the PRD's
   state machine literally, including price-dependent ones such as
   `MIN_RR`. A signal first touched at the far edge of its range is
   rejected even though it would pass a moment later nearer the zone. Only
   unverifiable or stale inputs, a clamped limit that cannot cross, and a
   stop already through the market make a signal wait.
4. **`bbo` does not push on a quiet market** (observed: none in 12 s on
   `xyz:GOLD` on a weekend; `l2Book` every ~5 s). A 2-second freshness rule
   on the stream alone would never enter. The bot re-confirms the top of
   book over REST (weight 2, at most once per 1.5 s per coin, only while a
   signal waits) and never decides on a quote older than the limit.
5. **Fee rate in sizing** is the larger of the account's `userCrossRate`
   and a configured constant (0.09 %, twice base taker) — the PRD's IV-11
   fallback applied as a floor.
6. **Paper default equity is 1,000 USDC.** With the PRD's defaults, an
   account above about 2,400 USDC sizes a 12-point-stop trade over the
   5,000 USDC notional cap and it is rejected, as specified ("never a
   smaller one"). Documented in the README and `.env.example`.
7. **Order state machine extensions** beyond the PRD diagram (each a
   transition it implies): `RESTING→FILLED`, `UNKNOWN→CANCELED`,
   `INTENT→NOT_ACCEPTED`, and fills / cancels arriving while a modify or
   cancel is in flight. Listed and tested in `tests/unit/test_states.py`.
8. **One database per environment** (`data/hlbot-<env>.sqlite3`), so paper
   records can never be mistaken for testnet or mainnet ones.
9. **Audit of exchange I/O** covers every signed action (request and
   response, by nonce). Read-only `info` calls are counted for rate-limit
   accounting but not individually audited.

## Task status

Columns: requirement · where it lives · depends on · acceptance criterion
(abridged from the PRD) · checks executed · status · known limitations.

### Phase 1 — Foundation

| Requirement | Location | Depends on | Acceptance | Checks executed | Status | Limitations |
|---|---|---|---|---|---|---|
| Project structure, packaging, entry point | `pyproject.toml`, `hlbot/` | — | Installs; `botctl` runs | `pip install -e .`; `botctl --version` | `[x]` | — |
| FR-095 config validation | `hlbot/config.py` | — | Malformed value stops startup naming the variable | U: 52 tests (`test_config.py`) | `[x]` | — |
| FR-082 mainnet guard | `hlbot/config.py`, `hlbot/cli.py` | FR-095 | Without flag **and** confirmation: paper, and says so | U: TC-122 ×3; C: CLI test | `[x]` | — |
| SR-03, SR-08 secrets handling | `hlbot/config.py` | — | Group/world-readable secret file refused; paper never loads a key | U: TC-121, key-file tests; C: paper adapter never opens the key file | `[x]` | Windows: permission bits not checked |
| Database, migrations, audit log (§12.1, NFR-008, NFR-009) | `hlbot/db/` | — | WAL + `synchronous=FULL`; audit rows cannot be updated or deleted; corrupt DB never treated as empty | C: 6 database tests | `[x]` | — |
| State machines (§12.2–12.4, §13.3) | `hlbot/domain/states.py`, `hlbot/db/database.py` | — | Every transition covered; terminal states immutable | U: 37 tests comparing the tables with the PRD diagrams | `[x]` | Extensions listed above |
| FR-091 structured logs, redaction (SR-05) | `hlbot/logging_setup.py`, `hlbot/redact.py` | — | Scan of logs after the suite finds no secret | C: TC-120 (logs, audit, outbox, all tables) | `[x]` | — |
| Baseline tests ported (NFR-018) | `tests/unit/*_ported.py`, `docs/TEST_PORT_MAPPING.md` | — | 393 tests ported or retired with a mapping | 68 ported verbatim pass; mapping written | `[x]` | — |
| Repository hygiene of the **old** repo (SEC-01…03, SR-14) | `../telegram-scraper` | Owner | Committed logs removed, permissions fixed, credentials rotated | Not done: out of this repository, and deleting or rewriting there needs the owner | `[!]` | Owner action |

### Phase 2 — Core features

| Requirement | Location | Depends on | Acceptance | Checks executed | Status | Limitations |
|---|---|---|---|---|---|---|
| FR-002, FR-003 channel-scoped identity, raw preservation | `core/signal_manager.py` | DB | Same id from two channels both stored; text byte-identical; `received_at` on every row | C: TC-062, `test_fr004_*` | `[x]` | — |
| FR-007, FR-008, FR-010 classify, extract, validate | `domain/parser.py`, `domain/validator.py` | — | Existing tests pass unchanged | U: 36 ported tests; C: TC-003 | `[x]` | — |
| FR-009 configurable channel parsers | `domain/parser.py`, `config.py` | — | Adding a channel with a known format is a config change only | C: `test_fr009_*` | `[x]` | — |
| FR-011, FR-039 duplicates | `core/engine.py` | DB | Same content same channel/day → `DUPLICATE`; second channel → `DUPLICATE_CROSS_CHANNEL` | C: TC-060, TC-061, TC-063, TC-064 | `[x]` | — |
| FR-020 expiry | `domain/planning.py`, `core/signal_manager.py` | — | Evaluated at/after expiry → `EXPIRED`, no order | U: TC-005; C | `[x]` | — |
| FR-022, FR-026 buffered range, TP ladder | `domain/planning.py` | — | Existing tests pass; split re-based on size increments and min notional | U: 32 ported + 28 new | `[x]` | — |
| FR-023, FR-024 event-driven zone check, freshness gate | `core/signal_manager.py`, `core/engine.py` | adapter streams | In-range tick triggers a decision; stale quote → wait with `DATA_STALE` | C: TC-006, `test_fr024_*`, TC-080; L: observed in paper mode | `[x]` | p95 over a replay harness not measured separately (see NFR-002) |
| FR-025 basis guard, price offset | `domain/risk.py`, `domain/planning.py` | — | Guard 3.0, deviation 4.0 → `BASIS_EXCEEDED` | U; C | `[x]` | Off by default |
| FR-030…FR-036, FR-038, FR-041, FR-042 sizing and risk controls (RC-01…RC-20) | `domain/risk.py` | — | Each control accepts at its limit and rejects beyond, naming the rule; unreadable input fails closed | U: 35 tests incl. FR-030, FR-036, FR-041 worked examples and TC-103 (1.26 / 0.60); C: TC-101, TC-102 | `[x]` | Liquidation price is an estimate from the documented formula. Daily percentage limits use the first managed dex's equity as their base (one dex is managed by default) |
| FR-037 leverage set and verified | `core/engine.py` | adapter | Exchange leverage equals configured value before any order | C: `test_fr037_*` | `[!]` | Real `updateLeverage` / `activeAssetData` round trip needs T |
| FR-040 precision and minimums | `domain/rounding.py`, `domain/risk.py` | — | No invalid tick, size or sub-minimum order reaches the exchange (zero `tickRejected` / `minTradeNtlRejected` in the **testnet** suite) | U: TC-037, TC-038, every rounded price passes the SDK's wire check; the simulator rejects invalid ticks and sizes and never did | `[!]` | Testnet assertion outstanding |
| FR-043, FR-044, FR-045, FR-046 kill switch, auto-pause, protective bypass | `core/health.py`, `core/engine.py`, `core/reconciler.py`, `core/app.py` | — | Pause persists; kill flat within 30 s or alert names what remains; each §10.7 condition pauses; missing SL replaced while paused | C: TC-090, TC-091, TC-092, `test_fr046_*`, one test per §10.7 condition | `[x]` | "Dex trading halted by deployer" is detected only through order rejections and the OI-cap list |
| FR-047 intent before send | `core/engine.py` | DB | Kill between persist and send → 0 or 1 exchange orders, never 2 | C: `test_fr047_*`, TC-070, TC-071, 100-kill soak | `[x]` | — |
| FR-048 entry order (limit IOC, cloid, `expiresAfter`) | `core/engine.py`, `exchange/hyperliquid.py` | — | Fill never worse than limit; nothing accepted after its deadline | C: TC-001, TC-002, TC-013; U: signed request shape matches §13.2 | `[!]` | Exchange-side enforcement needs T (IV-08) |
| FR-049 fill verification | `core/engine.py` | adapter | Local filled size equals the sum of exchange fills | C: every trade test; `test_nfr009_*` | `[x]` | — |
| FR-050, FR-051 SL and ladder placement | `core/engine.py` | — | SL covers 100 %; TP sizes sum to the position | C: TC-001, TC-030 | `[x]` | IV-03, IV-04 open |
| FR-052 protection verification | `core/engine.py` | — | `PROTECTED` only when the exchange's open orders show the stop; not protected by the hard deadline → flattened and alerted; **p95 ≤ 3 s on testnet** | C: TC-020…TC-023, TC-052 | `[!]` | Timing on testnet outstanding (7–17 ms in paper mode measures the bot's work only) |
| FR-053 partial entry fill | `core/engine.py` | — | 40 % fill under `accept` → SL and TPs sized to the 40 %; too small → flattened | C: TC-030, TC-031 | `[x]` | IV-07 |
| FR-054 coverage after each TP | `core/engine.py` | — | SL coverage re-confirmed after TP1 | C: `test_fr054_*`, TC-032 | `[x]` | — |
| FR-055, FR-056, FR-075 closure, outcome, P/L | `core/engine.py`, `domain/outcome.py` | — | No bot orders left; outcome matches fills; P/L = closed P/L − fees + funding to the cent | C: TC-024, TC-040…TC-044 | `[x]` | Simulator pays no funding: funding is tested with injected payments; real `userFunding` rows need T |
| FR-057 unknown-outcome resolution | `core/engine.py` | — | Response dropped → definite state, no second entry | C: TC-050 (two variants), TC-051, 6 resolution tests | `[x]` | — |
| FR-058 continuous reconciliation | `core/reconciler.py` | — | Out-of-band cancel reflected within 30 s | C: `test_fr058_*`, TC-081 | `[x]` | Default cadence 15 s |
| FR-059 startup recovery | `core/reconciler.py`, `core/app.py` | — | No entry before recovery completes; unprotected position protected or flattened first | C: TC-070…TC-074; L: `kill -9` and restart in paper mode, 0 requests sent for a protected trade | `[x]` | — |
| FR-060 foreign state | `core/reconciler.py`, `core/engine.py` | — | Manual position blocks entries and is untouched | C: 4 tests | `[x]` | — |
| FR-061 single instance | `core/app.py` | — | Second start exits non-zero within 2 s with a clear message | C: TC-076; L: exit code 3 in < 1 s, by `botctl start` and by direct launch | `[x]` | Lock is per data directory: two data directories on one account are not detected |
| FR-062 manual close | `core/engine.py`, `cli.py` | — | Flat and cleaned up within 30 s | C | `[x]` | — |
| FR-063 order modification | `core/engine.py` | — | After a modify exchange matches, or rolled back and alerted | C: `test_fr063_*` | `[-]` | Stop-loss only. TP modification is not implemented (Should) |
| FR-064 break-even after TP1 | `core/engine.py` | — | Off by default; when on, SL trigger equals average entry | C: 2 tests | `[x]` | — |

### Phase 3 — External integrations

| Requirement | Location | Depends on | Acceptance | Checks executed | Status | Limitations |
|---|---|---|---|---|---|---|
| Adapter interface (§13.1, NFR-016) | `exchange/base.py` | — | One interface; no SDK import elsewhere | U: architecture test | `[x]` | — |
| FR-021 instrument resolution | `exchange/hyperliquid.py`, `core/reconciler.py` | — | Asset id never hard-coded; unknown or delisted rejected; refreshed ≤ 5 min | U; C: TC-004, TC-086; L: `xyz:GOLD` → 110003, `PAXG` → 187 resolved live | `[x]` | — |
| FR-072 market data | `exchange/hyperliquid.py`, `exchange/hl_ws.py` | — | Quote age available for every decision | U: dispatch; C: local WS server (subscribe, ping/pong, reconnect, resubscribe, stale watchdog); L: live `bbo` / `activeAssetCtx` | `[x]` | Candles are not fetched (dashboard-only in the PRD). Order-book depth is not checked before entry (optional) |
| FR-070 account integration | `exchange/hyperliquid.py`, `core/reconciler.py` | Account, key | Startup fails closed if the account cannot be read or the signer is not authorised | U: stubbed REST incl. master-key refusal; C: startup failure tests; L: response shapes for an empty address | `[!]` | Needs T: populated positions, open orders, `userRole` for a real agent (IV-10, IV-13) |
| FR-081 testnet mode | `config.py`, `core/app.py` | Account, key | Switching is one config value; records labelled `TESTNET` | U; C: labels | `[!]` | Never started against testnet |
| Signed actions: place, modify, cancel, leverage (§13.2) | `exchange/hyperliquid.py` | Key | Shapes as documented; signed by the agent | U: 6 tests; signature recovers to the signer; **not sent** | `[!]` | Acceptance by the exchange needs T |
| Error mapping (§13.4) | `exchange/errors.py`, `core/engine.py` | — | Each error → mapped action | U: 16 texts; C: TC-085 (7 scenarios) | `[x]` | Matching is on documented text fragments; real strings need T |
| Rate limits (§9.6.3, NFR-014) | `core/action_queue.py`, `exchange/hyperliquid.py` | — | Backoff; protective actions first; headroom checked | C: TC-053, TC-054, headroom test | `[-]` | Weights accounted and exported; ≈ 170/min idle and ≈ 250/min while a signal waits are computed, not measured against the real limiter. No hard client-side limiter |
| Nonces (§9.6.2) | `core/action_queue.py` | — | One serialised queue; `max(now_ms, last + 1)` | C: strictly increasing, one writer | `[x]` | — |
| FR-080 paper mode | `exchange/paper.py`, `exchange/sim.py` | Market data | No key loaded; all checks run; labelled `PAPER` | C: 3 tests; L: full run against live data | `[x]` | Simulator: fills at touch, no depth, no funding |
| FR-001, FR-004, FR-005, FR-006 Telegram listener | `ingest/telegram.py` | Telegram credentials | Bad channel skipped; backfill on start/reconnect; edits alert; outage never ends the process | C: fake Telethon client: FR-001 ×2, TC-110 (40 failed reconnects), TC-111, backfill | `[!]` | Never connected to real Telegram. `botctl telegram-login` is untested |
| FR-092 notifications | `core/notifier.py` | Bot token | Retried until delivered or 24 h old; none lost across restart | C: TC-112, restart, 24 h drop, permanent rejection | `[!]` | The real Bot API call was never made |

### Phase 4 — UI/UX

| Requirement | Location | Depends on | Acceptance | Checks executed | Status | Limitations |
|---|---|---|---|---|---|---|
| FR-093 `botctl` | `hlbot/cli.py` | API | Every baseline verb has an equivalent; `config` hides secrets | C: 12 CLI tests incl. against a running bot; L: start, status, price, inject, positions, orders, pending, kill, resume, stop | `[x]` | `telegram-login` untested; `test` verb just runs pytest |
| FR-094 dashboard | `hlbot/api/` | — | Data age ≤ 5 s; loopback by default; control with confirmation and token | C: API test (status, equity, metrics, token, kill confirm, host check); the page's own script run under Node against a live payload (rendering, escaping, empty state); theme tokens from `enhancement-ui.md`. Light theme and logo setting: token and Node tests for both themes; logo default / upload / validation / reset over the API; switched, uploaded and reset in headless Chrome against a paper instance. Sign-in and roles (viewer / operator / admin; `hlbot/api/auth.py`, migration 0003): unit tests for hashing, sessions, expiry, throttling and the last-admin rule; an API test of first-time setup and what each role may do, including the cross-site checks; `botctl user` test. Pop-up settings and alerts, folding sections, menu highlight, dashboard name and daily win rate walked through in headless Chrome against a paper instance as admin, viewer and operator. Market volume (candles and 24-hour volume through the adapter, cached), trading analytics (`hlbot/api/analytics.py`) and the Excel export of the filtered history (`hlbot/api/xlsx.py`, standard library only): hand-checked data sets for every key figure, filters, time zones, paging and the workbook; rendered and downloaded in headless Chrome against a paper instance. Rendered in headless Chrome against the running testnet bot at 390, 768, 1100 and 1440 px: no horizontal overflow, screenshots reviewed | `[x]` | Redesigned 2026-10-11 to the supplied dark theme. The pause/resume/kill confirm-and-token flow was not clicked through in a browser. Fonts fall back to the system font unless Plus Jakarta Sans or Inter is installed: nothing is loaded from another origin |
| FR-073, FR-074 position monitoring, liquidation proximity | `core/reconciler.py`, `core/engine.py` | — | Values equal the latest snapshot, age shown; alert once per crossing | C | `[x]` | — |
| FR-071 health model | `core/health.py` | — | Every transition audited; PAUSED/HALTED alert | C | `[x]` | — |

### Phase 5 — Security and reliability

| Requirement | Location | Checks executed | Status | Limitations |
|---|---|---|---|---|
| SR-01, NFR-010 agent key only | `exchange/hyperliquid.py` | U: master key refused; key never in payload, logs | `[x]` | — |
| SR-02 key expiry tracking | `core/reconciler.py` | C: warn at 14 days, pause inside 24 h | `[!]` | `extraAgents` expiry for a real agent needs T |
| SR-04 Telethon session | `ingest/telegram.py` | C: `0644` session refused; private directory | `[x]` | — |
| SR-06, NFR-011 no transfer code path | whole package | U: architecture test over the source | `[x]` | IV-01 open |
| SR-09 API on loopback, control token | `api/server.py` | C | `[x]` | — |
| SR-11 `.gitignore` | `.gitignore` | U | `[x]` | Not a git repository yet |
| SR-12 pinned dependencies with hashes | `requirements.lock`, `requirements-dev.lock`, `scripts/make_lock.py` | Fresh `pip install --require-hashes` on 3.11 and 3.13 (macOS), 3.12 and 3.13 (Linux container); `pip check` clean | `[x]` | Hashes are PyPI's published sha256 digests for every file of each pinned version |
| SR-13 placeholders only | `.env.example`, tests | U | `[x]` | — |
| NFR-004 never unprotected beyond the hard deadline | `core/engine.py` | C: TC-021, TC-022, TC-025, soak | `[x]` | — |
| NFR-006 divergence detection | `core/reconciler.py` | C | `[x]` | — |
| NFR-015 clock drift | `core/reconciler.py` | C | `[x]` | Estimated from exchange timestamps over a one-minute window |
| NFR-022 privacy in logs | `redact.py` | C: TC-120 | `[x]` | — |
| SR-07, SR-14, OQ-16 | Owner | — | `[!]` | Operator practice |

### Phase 6 — Testing and quality

| Check | Command | Result (2026-10-11; CPython 3.13.9 on macOS arm64 unless stated) | Status |
|---|---|---|---|
| Lint | `ruff check hlbot tests` | All checks passed | `[x]` |
| Types | `mypy` | Success: no issues found in 40 source files | `[x]` |
| Tests | `pytest` | 503 passed on 3.13.9; 503 passed on 3.11.9 (macOS); 503 passed on 3.12.14 (Linux aarch64 container, with lint and types) | `[x]` |
| Coverage (NFR-017: ≥ 90 % on risk, sizing, planning, state machine, reconciliation) | `pytest --cov=hlbot` | `domain/risk` 98 %, `domain/planning` 98 %, `domain/states` 100 %, `core/reconciler` 93 %, `core/engine` 91 %, `db/database` 92 %; those modules together 93 %; 91 % overall | `[x]` |
| Crash injection (NFR-007, TC-075) | `tests/component/test_crash_soak.py` | 100 kills at random points: 0 duplicate entries, 0 positions unprotected after recovery — **against the simulator** | `[!]` on testnet |
| NFR-001 message → decision | metric `signal_to_intent_ms` | C: p95 < 3 s over 30 trades; L: 3 ms with a fresh quote | `[-]` soak outstanding |
| NFR-002 quote → order submitted | metric `decision_to_submit_ms` | C: p95 < 250 ms over 30 trades | `[x]` |
| NFR-003 first fill → `PROTECTED` p95 ≤ 3 s, ≥ 50 testnet trades | metric `time_to_protected_ms` | Not measured on testnet | `[!]` |
| NFR-021 capacity | `test_nfr021_*` | 2,000 messages (10× a day's volume) ingested at well under 50 ms each | `[x]` |
| NFR-020 Linux and macOS | by hand; `.github/workflows/ci.yml` | macOS: suite on 3.11 and 3.13, paper run. Linux: suite on 3.12 in a container, and the built image run in paper mode (start, inject, protected, kill, SIGTERM stop). The CI workflow file itself has never executed | `[x]` |

Test-case coverage against PRD §16.2:

| TC | Level reached | Test |
|---|---|---|
| TC-001, 002 | C | `test_entry.py` |
| TC-003 | U, C | ported validator tests; `test_non_signal_messages_*` |
| TC-004, 005, 006 | U, C | `test_entry.py`, `test_planning.py` |
| TC-010, 012 | — | Testnet only |
| TC-011, 013 | C | `test_entry.py` |
| TC-020…025 | C | `test_protection.py` |
| TC-030, 031, 032 | C | `test_protection.py` |
| TC-035, 037, 038 | U | `test_risk.py`, `test_rounding.py` |
| TC-036 | C | `test_entry.py` |
| TC-040…044 | C | `test_outcomes.py` |
| TC-050, 051, 052 | C | `test_recovery.py`, `test_protection.py` |
| TC-053, 054 | C | `test_reconcile.py` |
| TC-060…064 | C | `test_entry.py` |
| TC-070…074, 076 | C | `test_recovery.py` |
| TC-075 | C only | `test_crash_soak.py` |
| TC-080, 081 | C | `test_reconcile.py` |
| TC-082 | — | Testnet only. The client-side equivalent (silent socket → reconnect) is in `test_adapter.py` |
| TC-085, 086 | C | `test_edges.py`, `test_entry.py` |
| TC-090, 091, 092 | C | `test_recovery.py`, `test_reconcile.py` |
| TC-100…103 | U, C | `test_risk.py`, `test_recovery.py` |
| TC-110, 111, 112 | C | `test_infra.py` |
| TC-120, 121, 122 | C, U | `test_infra.py`, `test_config.py` |

Every case the PRD marks "Testnet" is outstanding at that level.

### Phase 7 — Deployment readiness

| Item | Location | Checks executed | Status | Limitations |
|---|---|---|---|---|
| Local setup, `.env.example`, README | `README.md`, `.env.example` | Followed for the paper run | `[x]` | — |
| Supervisor (NFR-005) | `deploy/hlbot.service` | Not installed or run | `[-]` | Written from the systemd manual; untested |
| Container | `Dockerfile`, `docker-compose.yml` | Image built from the lockfile (235 MB, uid 10001); run read-only with all capabilities dropped: healthy, signal entered and protected, kill, exit 0 on SIGTERM; bad config exits 2. Compose file validated with `docker compose config` | `[-]` | `docker compose up` itself was not run (needs a real `.env` and secrets directory) |
| Health check, metrics (NFR-012) | `GET /health`, `GET /metrics`, `data/heartbeat` | C; L | `[x]` | External heartbeat monitor not included (Should) |
| Backups (NFR-008) | `botctl backup` | C: consistent copy, owner-only | `[-]` | Scheduling is left to cron; restore drill not performed |
| Deploy / rollback (NFR-019) | `docs/RUNBOOK.md` | Not drilled | `[-]` | — |
| CI | `.github/workflows/ci.yml` | Never executed | `[-]` | — |

## Outstanding work, in order

1. **Close the integration-verification list on testnet** (gate G2). Needs
   a testnet account funded through the faucet (which requires a mainnet
   deposit from the same address) and an agent key. Steps are in
   [docs/INTEGRATION_VERIFICATION.md](docs/INTEGRATION_VERIFICATION.md).
   Expect small adapter fixes: populated response rows and real error
   strings have only been tested against documented shapes.
2. **Connect real Telegram** (`botctl telegram-login`) and run the paper
   soak (gate G3) on live signals; measure the offset between the
   signal's levels and the Hyperliquid market before any mainnet trade
   (PRD R-01, OQ-02).
3. **Open the dashboard in a browser** and review it.
4. **Push to a repository so the CI workflow runs.** Its steps were run
   by hand here; the workflow file has never executed. This directory is
   not a git repository yet.
5. **Owner decisions** in PRD §19, especially OQ-01, OQ-02, OQ-07, OQ-08.
6. **Old repository hygiene:** remove committed logs, fix `.env` and
   session permissions, rotate the MT5 password, API hash and bot token
   (SEC-01…03, SR-14).
7. Not built, by design or priority: TP order modification (FR-063, in
   part), Telegram bot commands (OQ-11), external heartbeat monitor,
   trading-hours window (R-16), order-book depth check, candles.

## Resuming

The repository is in a consistent state: lint, types and tests are green.
To continue, start at item 1 above. Useful entry points:
`hlbot/core/engine.py` (trade lifecycle), `hlbot/exchange/hyperliquid.py`
(the only code that will need changes when testnet behaviour differs from
the assumptions), `hlbot/exchange/sim.py` (encode whatever testnet shows
so the component tests keep matching reality).
