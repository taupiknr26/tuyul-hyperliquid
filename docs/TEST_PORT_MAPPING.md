# Test port mapping (NFR-018)

The baseline (`telegram-scraper`, commit `998844e`) has 393 unit tests in
25 files. Every one is accounted for below as **ported** (runs here with
only its import path changed), **re-expressed** (the same behaviour is
tested against the new design), or **retired** (the thing it tested no
longer exists; the successor coverage is named).

| | Tests |
|---|---|
| Ported verbatim | 68 |
| Re-expressed | 97 |
| Retired with successor coverage | 228 |
| **Baseline total** | **393** |
| Tests in this repository | 496 |

## Ported verbatim (68)

| Baseline file | Tests | Here | Notes |
|---|---|---|---|
| `test_signal_parser.py` | 26 | `tests/unit/test_parser_ported.py` | Import path only. The entry-limit channel id is now set with `configure_channel_parsers()` in `setUpModule`, because the mapping moved from source to configuration (LIM-21). `PARSER_VERSION` is 1.2.1; the tests compare against the constant. Added `TestBuyZoneWrittenTopDown` (not in the baseline) |
| `test_signal_validator.py` | 10 | `tests/unit/test_validator_ported.py` | Import path only |
| `test_execution_plan.py` | 32 of 40 | `tests/unit/test_planning_ported.py` | Effective range, executable price, signal id, expiry, TP allocation and buffer, slippage, level validation. `build_signal_id` is kept as an alias of `build_content_id` |

## Re-expressed (97)

| Baseline file | Tests | Here | What changed |
|---|---|---|---|
| `test_position_sizing.py` | 15 | `tests/unit/test_risk.py` | `sizing_equity` and the daily target keep their behaviour. `compute_risk_based_volume` became `compute_size`: no contract size or lot step; the distance adds the SL slippage allowance and both fees; rounding is down to `szDecimals` (FR-030 example reproduced) |
| `test_signal_outcome.py` | 15 | `tests/unit/test_misc_domain.py` (outcome tests), `tests/component/test_outcomes.py` | Classification is from the role of the order that produced each closing fill, not from MT5 deal comments. A manual close is `MANUAL`, no longer a loss (LIM-11). New outcomes `FLATTENED`, `LIQUIDATED` |
| `test_telegram_notifier.py` | 28 | `tests/unit/test_config.py` (7 config cases), `tests/component/test_infra.py` (formatters, outbox) | Formatters take trade/order/fill data and carry an environment label. Sending goes through a persistent outbox with retry instead of send-and-drop (LIM-10). The formatter cases are covered by two broader tests rather than one per field |
| `test_execution_config.py` | 21 | `tests/unit/test_config.py` | Same intent (defaults, overrides, blank values, malformed values name the variable). Risk limits are on by default instead of off (LIM-07). The "only MARKET" and "only PRICE_POINTS" cases are gone with those settings |
| `test_payload_config.py` | 5 | `tests/unit/test_config.py` | The symbol-map JSON cases carry over as `HL_SYMBOL_MAP`. Volume and magic number are retired |
| `test_auto_config.py` | 4 | `tests/unit/test_config.py` | `AUTO_POLL_INTERVAL` is the reconciliation cadence. `AUTO_WATCH_INTERVAL` is retired (no file watcher) |
| `test_message_scraper.py` | 4 | `tests/component/test_infra.py` (`test_fr001_*`) | One bad channel does not lose the others; all bad raises. Extended with backfill, reconnect, edits and deletes |
| `test_main.py` | 5 | `tests/component/test_infra.py` (`test_start_and_stop_*`) | Stop flag and started/stopped notifications |

## Retired (228)

| Baseline file | Tests | Why | Successor coverage |
|---|---|---|---|
| `test_execution_plan.py` — `TestSplitVolume` | 8 | Lots and minimum lot replaced by size increments and a minimum notional per order | `tests/unit/test_planning.py::TestSplitSizeUnits` |
| `test_state_manager.py` | 9 | JSON state files replaced by SQLite | `tests/component/test_infra.py`: duplicates rejected by primary key, a corrupt database is never treated as empty (LIM-14), terminal states immutable |
| `test_json_writer.py`, `test_parsed_writer.py`, `test_validated_writer.py`, `test_payload_writer.py`, `test_execution_writer.py` | 38 | The `.json` / `.jsonl` writers are gone (LIM-19) | Database tests; `test_fr004_*` checks messages are stored verbatim exactly once |
| `test_run_validator.py`, `test_run_payload_builder.py`, `test_run_executor.py` | 28 | File-coupled phase runners replaced by in-process events. `--force` (LIM-13) deliberately has no equivalent | `tests/component/test_entry.py` (expiry, duplicates TC-060…TC-064), `test_infra.py` (invalid signals stored with reasons) |
| `test_mt5_payload_builder.py` | 24 | MT5 payloads | Planning is tested through the signal manager in `test_entry.py`, `test_infra.py` (`test_fr009_*`, price offset in `test_planning.py`) |
| `test_mt5_client.py` | 9 | MT5 terminal connection | `tests/unit/test_adapter.py` |
| `test_mt5_executor.py` | 78 | The MT5 executor is replaced. Its themes map as follows: range and waiting → `test_entry.py`; one order per TP leg → one entry + SL + ladder (`test_tc001_*`); SL read-back and re-apply → protection protocol (`test_protection.py`); slippage action → hard limit price (`TestEntryLimitPrice`); risk rejections → `test_risk.py`; retryable rejections → `test_edges.py`; unknown `order_send` result → `test_recovery.py` (now resolved instead of left as `error`, LIM-02); dry run → paper mode with full checks (LIM-15) |
| `test_position_monitor.py` | 11 | Poll-until-closed monitor | `test_outcomes.py` (always on, driven by fills; LIM-12) |
| `test_orchestrator.py` | 23 | File-watch pipeline loop, in-memory daily flags | `test_infra.py` (lifecycle, daily target once per day across restart, LIM-16), `test_reconcile.py` |
| `fake_mt5.py` | — | Fake terminal | `hlbot/exchange/sim.py`, the scriptable simulated exchange |
