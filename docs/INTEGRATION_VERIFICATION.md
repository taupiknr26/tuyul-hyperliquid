# Integration verification — what is and is not proven

The PRD (§9.7) lists fourteen Hyperliquid behaviours the design depends on
that the official documentation does not fully specify. Each must be
proven on **testnet** before the requirement that depends on it is
accepted (gate G2).

**None of them is fully closed.** The code was built without an account;
a first testnet trade has since been placed (next section), which
confirms parts of five items. This page records, for each item, what the code assumes, what
happens if the assumption is wrong, and how to check it.

## First testnet evidence (2026-10-10 UTC)

One trade has now been placed on testnet (PAXG, default dex, account with
abstraction disabled): BUY 0.02 by limit IOC, filled at 4,213.9; stop at
4,202 and five reduce-only take-profits accepted; `PROTECTED` 1.8 s after
the fill; the bot's view matched the exchange's own position and open
orders exactly. This is one sample, on one market, on the happy path.

| Now observed on the real API | Still not observed |
|---|---|
| Signer check through `userRole` (agent of the account) | Agent expiry from `extraAgents` |
| `updateLeverage` 10 → 5 and its read-back | Isolated margin |
| Signed `order` actions accepted with our nonce, cloid and `expiresAfter` | `batchModify`, `cancelByCloid` |
| IV-08 (in part): a fully filled IOC returns `filled {totalSz, avgPx, oid}` | Partial and zero-fill IOC |
| IV-04 (in part): five resting reduce-only TPs are accepted next to a full-size stop | What happens to them as the position shrinks or closes |
| IV-10 (in part): with abstraction disabled, per-dex `clearinghouseState` is the usable balance and agrees with `activeAssetData.availableToTrade` | Unified-account / dex-abstraction modes; any HIP-3 dex with funds |
| IV-11 (in part): taker fee charged was 0.045 % on PAXG; sizing used the 0.09 % floor | Fees on a HIP-3 market |
| IV-12: requests signed from CPython 3.13 are accepted | — |
| Populated `frontendOpenOrders` rows ("Stop Market", "Limit"), positions, and `userFills` carrying our cloid over the WebSocket | Trigger firing (IV-14), tiny remainders (IV-07), weekend behaviour (IV-02), sub-accounts (IV-13), IV-01 |

Two defects surfaced on first contact and were fixed with tests: an
unfunded dex was reported as "daily loss limit reached", and a
self-clearing block about a market that was no longer configured survived
a restart.

## What has been verified, and how

| Evidence | What it shows | What it does not show |
|---|---|---|
| 503 automated tests against the simulated exchange | The engine's logic on every path the PRD's test plan names: entry, protection, partial fills, unknown outcomes, crash recovery, reconciliation, kill switch | Anything about the real exchange. The simulator encodes the assumptions below |
| Read-only adapter run against mainnet (`perpDexs`, `meta`, `metaAndAssetCtxs`, `l2Book`, `perpsAtOpenInterestCap`; WebSocket `bbo`, `activeAssetCtx`, `l2Book`) on 2026-10-11 | Response parsing, runtime asset-id resolution (`xyz:GOLD` → 110003), WebSocket connect / subscribe / ping-pong | User-data endpoints with a real account |
| Shapes of `clearinghouseState`, `frontendOpenOrders`, `orderStatus`, `userFills`, `userFees`, `userRateLimit`, `userRole`, `extraAgents`, `activeAssetData` read for an empty address | The fields the adapter parses exist with those names | Populated values (open orders, positions, fills) — those parsers are tested against shapes taken from the documentation |
| Signing with a throwaway in-memory key, **not sent** | The SDK builds and signs `order`, `batchModify`, `cancelByCloid`, `updateLeverage` with our nonce and `expiresAfter`; the wire format matches PRD §13.2; the signature recovers to the signer | That the exchange accepts them |
| Paper mode run end to end against live mainnet data, including `kill -9` and restart | The full process: start, single-instance lock, signal → entry → SL + ladder → kill → stop; recovery adopts an open trade and sends nothing | Real fills, triggers, rate limits, signing |
| SDK 0.24.0 installs from the hash-pinned lockfile and the whole suite passes on CPython 3.11.9 and 3.13.9 (macOS arm64) and 3.12.14 (Linux aarch64); the container image runs paper mode on Linux | Strong evidence for IV-12 on the install and signing side | That signed requests from those builds are accepted by the exchange |

## The fourteen items

| ID | Open question | What the code does today | If the assumption is wrong | How to close it on testnet |
|---|---|---|---|---|
| IV-01 | Can an agent key sign withdrawals or transfers? | Assumes nothing. No such call exists in the code (enforced by a test) | A leaked key could move funds through another tool | Try a `usdSend` signed by the agent with a separate script; keep only trading capital in the account either way |
| IV-02 | Mark price and trigger behaviour on a HIP-3 market when the deployer's oracle is stale or the underlying is closed | No special handling. No trading-hours window | A stop may not trigger, or trigger late, over a weekend | Hold a minimum-size position with a stop across a weekend close; add a trading-hours window or switch `HL_SYMBOL_MAP` to `PAXG` if unreliable |
| IV-03 | Exact semantics of `positionTpsl` | **Not used.** The stop is a fixed-size reduce-only trigger, resized by `modify` after each TP fill (the PRD's fallback), using the same cloid | If `modify` cannot keep the cloid, the stop is found "missing" and re-placed with a new cloid — covered, but slower | Place SL, fill a TP, check the modified order with `frontendOpenOrders` |
| IV-04 | Are resting reduce-only TPs accepted when their sum plus the SL exceeds the position? Are they resized or cancelled as it shrinks? | Assumes accepted, and that a reduce-only order larger than the position is capped at the position. Never rests more TP size than the position | TPs are rejected. This never causes a flatten: the stop still covers the position. Switch to `HL_TP_STYLE=trigger` | Place the ladder next to a full-size SL and read back open orders |
| IV-05 | Is re-sending an identical signed request rejected as a used nonce? | Never re-sends. An unknown outcome is resolved by lookup only | None: not relied on | Optional |
| IV-06 | Is a duplicate `cloid` rejected? | Not relied on. Cloids are unique in the database and derived deterministically | None | Optional |
| IV-07 | Does the 10 USDC minimum apply to a reduce-only order that closes a remainder? | Assumes **yes** (conservative): a fill too small for one minimum order is flattened, and a TP remainder under the minimum is left to the stop | If a tiny remainder cannot be closed, the flatten is rejected and the trade ends `HALTED_MANUAL` with a repeating alert | Open the minimum size, close most of it, try to close the rest |
| IV-08 | What does a partially filled IOC return? A zero-fill IOC? | Never trusts the response alone: filled size is confirmed from `orderStatus` and fills. A partial fill follows `EXEC_PARTIAL_FILL_POLICY` | None expected | Send an IOC larger than the top of book |
| IV-09 | Does `scheduleCancel` cancel trigger orders? | Never calls it (enforced by a test) | None | Not needed |
| IV-10 | How does USDC collateral reach a HIP-3 dex, and which endpoint reports usable balance in each account mode? | Standard mode: reads `clearinghouseState` with `dex` and uses that dex's `accountValue`. **Unified account** (`userAbstraction` = `unifiedAccount`, observed on mainnet 2026-10-11 with a flat account): every per-dex account value is 0 and the collateral is the spot USDC balance, so the bot reads `spotClearinghouseState` and uses USDC `total` (less any unrealised loss; a gain is not counted) as equity for every managed dex, and `total - max(hold, margin used)`, capped by `tokenToAvailableAfterMaintenance`, as free margin. The bot never moves collateral | Unified mode with an open position has not been observed: if the exchange reports held margin differently, free margin could be misjudged (the rules err low). Other modes (dex abstraction, portfolio margin) are not handled: the balance reads as zero and every entry is blocked (fails closed) | Open a minimum-size position in unified mode and compare `botctl positions` and the dashboard's account panel with the Hyperliquid UI |
| IV-11 | The taker/maker fee actually charged on the chosen market | Uses the larger of `userFees.userCrossRate` and `HL_FEE_RATE_FALLBACK` (0.09 %, twice the base taker) | If HIP-3 fees are higher than both, risk per trade is slightly understated | Compare the `fee` on a testnet fill with the figure in the risk assessment |
| IV-12 | Which CPython versions the SDK works on | Pinned `>=3.11,<3.14`; install, signing and the suite verified on 3.11, 3.12 and 3.13 | Install or signing failure at startup | Covered once any order is accepted on testnet |
| IV-13 | Can an agent trade a sub-account through `vaultAddress` on a HIP-3 dex? | `HL_VAULT_ADDRESS` is supported and signed into the request; untested against the exchange | Orders rejected | Prefer a dedicated wallet (the PRD's recommendation); otherwise test one order |
| IV-14 | When a stop triggers, which order id carries the fills? | Fills are matched by cloid, then by oid. If a closing fill matches no bot order and the exchange reports our stop as `triggered` / `filled`, the fill is attributed to the stop | Without that fallback a stopped-out trade would be labelled `MANUAL` instead of `LOSS_ALL`; P/L is unaffected | Let a stop trigger and run `botctl trace --trade N` |

## Other things only the real exchange can show

- **Populated user-data parsing.** `assetPositions`, `frontendOpenOrders`
  rows (`orderType` strings such as "Stop Market"), `orderStatus` for a
  real order, the `orderUpdates` and `userEvents` streams.
- **Signer authorisation.** `userRole` returning `agent` with the
  account's address, and agent expiry from `extraAgents`.
- **Error strings.** Mapping is by fragments of the documented texts
  (`hlbot/exchange/errors.py`); an unrecognised text is treated as a
  rejection of that one request, never as success.
- **Latency.** Time-to-protected was 7–17 ms in paper mode, which measures
  only the bot's own work. NFR-003 (p95 ≤ 3 s over ≥ 50 testnet trades) is
  not measured.
- **Rate limits.** Weights are accounted and exported; the budget
  (≈ 170 weight/min idle, ≈ 250 while a signal waits on a quiet market)
  is computed, not observed against the real limiter.

## Suggested order on testnet

1. `HL_ENV=testnet`, `botctl start`: closes startup, signer check, IV-10,
   IV-12.
2. `botctl inject` a signal at the current price with the minimum viable
   size: closes IV-08, IV-11, and the populated parsers.
3. Read back open orders: IV-03, IV-04.
4. Move the price reference so a TP fills, then let the stop trigger:
   IV-14, IV-07.
5. `kill -9` the process at several points and restart (TC-070…TC-075 on
   the real API).
6. Leave a minimum-size position over a weekend: IV-02.
