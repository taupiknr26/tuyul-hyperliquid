"""Hyperliquid adapter: response parsing, request shapes, signing through
the official SDK, error mapping, and the WebSocket lifecycle. No network:
REST is stubbed and the WebSocket talks to a local test server."""

import asyncio
import json
import secrets
from datetime import UTC, datetime

import pytest
from aiohttp import web
from hyperliquid.utils.error import ClientError, ServerError
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

from hlbot.domain.cloid import make_cloid
from hlbot.exchange.base import ErrorClass, ExchangeError, OrderRequest, OutcomeUnknown, StreamHealth
from hlbot.exchange.hl_ws import HyperliquidWs, next_backoff
from hlbot.exchange.hyperliquid import (
    HyperliquidAdapter,
    builder_dex_offset,
    parse_fill,
    parse_open_order,
    parse_order_status,
    parse_place_statuses,
)
from hlbot.timeutil import ManualClock

ACCOUNT = "0x" + "1a" * 20
COIN = "xyz:GOLD"

META_XYZ = {"universe": [
    {"szDecimals": 2, "name": "xyz:AAPL", "maxLeverage": 10},
    {"szDecimals": 3, "name": "xyz:OLD", "maxLeverage": 5, "isDelisted": True},
    {"szDecimals": 1, "name": "xyz:ISO", "maxLeverage": 3, "onlyIsolated": True},
    {"szDecimals": 4, "name": "xyz:GOLD", "maxLeverage": 25, "marginTableId": 25},
]}
PERP_DEXS = [None, {"name": "xyz"}, {"name": "flx"}]
FILL = {"coin": COIN, "px": "4287.0", "sz": "0.2412", "side": "A", "time": 1791652503470, "startPosition": "0.0", "dir": "Open Short",
        "closedPnl": "0.0", "hash": "0x" + "ab" * 32, "oid": 5001, "crossed": True, "fee": "0.4653", "tid": 77, "feeToken": "USDC",
        "cloid": "0x" + "c" * 32}
OPEN_SL = {"coin": COIN, "side": "B", "limitPx": "4725.6", "sz": "0.2412", "oid": 5002, "timestamp": 1791652503900, "triggerCondition": "Price above 4296",
           "isTrigger": True, "triggerPx": "4296.0", "children": [], "isPositionTpsl": False, "reduceOnly": True, "orderType": "Stop Market",
           "origSz": "0.2412", "tif": None, "cloid": "0x" + "d" * 32}
OPEN_TP = {"coin": COIN, "side": "B", "limitPx": "4283.0", "sz": "0.0484", "oid": 5003, "timestamp": 1791652503901, "triggerCondition": "N/A",
           "isTrigger": False, "triggerPx": "0.0", "children": [], "isPositionTpsl": False, "reduceOnly": True, "orderType": "Limit",
           "origSz": "0.0484", "tif": "Gtc", "cloid": "0x" + "e" * 32}


class Stub(HyperliquidAdapter):
    """Adapter whose HTTP layer is a scripted function."""

    def __init__(self, responder, **kwargs):
        defaults = dict(rest_url="https://example.invalid", ws_url="ws://example.invalid", is_mainnet=False, coins=[COIN, "PAXG"])
        super().__init__(**{**defaults, **kwargs})
        self.calls = []
        self._responder = responder

    def _post_sync(self, path, payload):
        self.calls.append((path, payload))
        result = self._responder(path, payload)
        if isinstance(result, Exception):
            raise result
        return result


def info_responder(path, payload):
    kind = payload["type"]
    if kind == "perpDexs":
        return PERP_DEXS
    if kind == "meta":
        return META_XYZ if payload["dex"] == "xyz" else {"universe": [{"szDecimals": 3, "name": "PAXG", "maxLeverage": 10}]}
    raise AssertionError(payload)


# ---------------------------------------------------------------- pure parsing
def test_asset_id_rule_for_builder_dexes():
    assert builder_dex_offset(0) == 0 and builder_dex_offset(1) == 110000 and builder_dex_offset(2) == 120000


def test_parse_fill_with_and_without_optional_fields():
    fill = parse_fill(FILL)
    assert (fill.tid, fill.oid, fill.coin, fill.is_buy, fill.price, fill.size) == (77, 5001, COIN, False, 4287.0, 0.2412)
    assert fill.fee == 0.4653 and fill.cloid == "0x" + "c" * 32 and not fill.liquidation and fill.crossed
    liquidated = parse_fill({**FILL, "cloid": None, "liquidation": {"liquidatedUser": ACCOUNT, "markPx": "4400", "method": "market"}})
    assert liquidated.liquidation and liquidated.cloid is None


def test_parse_open_orders_identifies_stops_and_plain_limits():
    sl = parse_open_order(OPEN_SL)
    assert sl.is_trigger and sl.tpsl == "sl" and sl.reduce_only and sl.is_buy and sl.trigger_px == 4296.0 and sl.size == 0.2412
    tp = parse_open_order(OPEN_TP)
    assert not tp.is_trigger and tp.tpsl is None and tp.trigger_px is None and tp.price == 4283.0
    tp_trigger = parse_open_order({**OPEN_SL, "orderType": "Take Profit Market", "triggerPx": "4283.0"})
    assert tp_trigger.tpsl == "tp"


def test_parse_order_status_found_and_unknown_oid():
    found = parse_order_status({"status": "order", "order": {"order": {"coin": COIN, "oid": 5001, "sz": "0.1", "origSz": "0.2412", "cloid": "0x" + "c" * 32},
                                                              "status": "canceled", "statusTimestamp": 1791652504000}})
    assert found.found and found.status == "canceled" and found.filled_size == pytest.approx(0.1412)
    assert not parse_order_status({"status": "unknownOid"}).found


def test_parse_place_statuses_all_shapes():
    response = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"resting": {"oid": 11}}, {"filled": {"totalSz": "0.2412", "avgPx": "4287.0", "oid": 12}}, {"error": "Invalid TP/SL price."}]}}}
    resting, filled, error = parse_place_statuses(response, 3)
    assert resting.status == "resting" and resting.oid == 11
    assert filled.status == "filled" and filled.total_size == 0.2412 and filled.avg_price == 4287.0
    assert error.status == "error" and error.error.kind == ErrorClass.INVALID_TPSL


def test_a_single_prevalidation_error_applies_to_the_whole_batch():
    response = {"status": "ok", "response": {"data": {"statuses": [{"error": "Order must have minimum value of $10."}]}}}
    results = parse_place_statuses(response, 3)
    assert len(results) == 3 and all(r.status == "error" and r.error.kind == ErrorClass.LOCAL_BUG for r in results)


def test_an_unexpected_response_shape_is_an_unknown_outcome_never_a_success():
    with pytest.raises(OutcomeUnknown):
        parse_place_statuses({"status": "ok", "response": {"type": "default"}}, 1)
    with pytest.raises(OutcomeUnknown):
        parse_place_statuses({"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}, {"resting": {"oid": 2}}]}}}, 1)


# ---------------------------------------------------------------- read paths
async def test_instruments_resolve_asset_ids_at_runtime_for_each_dex():
    adapter = Stub(info_responder)
    instruments = await adapter.get_instruments()
    gold = instruments[COIN]
    assert (gold.asset_id, gold.sz_decimals, gold.max_leverage, gold.dex) == (110003, 4, 25, "xyz")
    assert instruments["PAXG"].asset_id == 0 and instruments["PAXG"].dex == ""
    assert instruments["xyz:OLD"].is_delisted and instruments["xyz:ISO"].only_isolated
    assert adapter.rest_weight_last_minute() == 60  # perpDexs + two meta calls at weight 20
    health = adapter.stream_health()
    assert health.rest_ms is not None and health.rest_ms >= 0 and health.rest_avg_ms is not None  # every REST call is timed
    assert len(adapter._latencies) == 3 and health.ping_ms is None  # no stream yet: nothing to report
    await adapter.stop()


async def test_account_is_read_per_dex_with_the_account_address_not_the_agent():
    key = "0x" + secrets.token_hex(32)

    def responder(path, payload):
        assert payload["user"] == ACCOUNT
        if payload["type"] == "userAbstraction":
            return "disabled"
        if payload["dex"] == "xyz":
            return {"marginSummary": {"accountValue": "1000.5", "totalNtlPos": "1034.0", "totalMarginUsed": "206.8"}, "crossMaintenanceMarginUsed": "20.7",
                    "withdrawable": "793.7", "time": 1791652503798,
                    "assetPositions": [{"type": "oneWay", "position": {"coin": COIN, "szi": "-0.2412", "entryPx": "4287.0", "unrealizedPnl": "0.5",
                                                                        "leverage": {"type": "cross", "value": 5}, "liquidationPx": "8300.1", "marginUsed": "206.8",
                                                                        "cumFunding": {"allTime": "0.1", "sinceOpen": "0.02", "sinceChange": "0.0"}}}]}
        return {"marginSummary": {"accountValue": "0.0", "totalNtlPos": "0.0", "totalMarginUsed": "0.0"}, "withdrawable": "0.0", "assetPositions": [], "time": 1791652503799}

    adapter = Stub(responder, account_address=ACCOUNT.upper().replace("0X", "0x"), agent_key=key)
    account = await adapter.get_account()
    xyz = account.dex_account("xyz")
    assert xyz.account_value == 1000.5 and xyz.margin_used == 206.8 and xyz.available_margin == pytest.approx(793.7)
    position = account.position(COIN)
    assert position.szi == -0.2412 and position.leverage == 5 and position.liquidation_px == 8300.1 and position.cum_funding == 0.02
    assert account.szi("PAXG") == 0.0 and account.exchange_ms == 1791652503799
    assert not account.unified and account.accounts_once() == account.by_dex
    assert adapter.rest_weight_last_minute() == 4 + 20  # clearinghouseState is weight 2 each; the mode lookup is charged the default 20
    await adapter.get_account()
    assert [p["type"] for _path, p in adapter.calls].count("userAbstraction") == 1  # the mode is cached for a minute
    await adapter.stop()


# ---------------------------------------------------------------- unified account (IV-10)
def _unified_responder(spot, mode="unifiedAccount", xyz=None):
    """Shapes as mainnet returned them for a flat unified account on 2026-10-11."""
    flat = {"marginSummary": {"accountValue": "0.0", "totalNtlPos": "0.0", "totalRawUsd": "0.0", "totalMarginUsed": "0.0"}, "withdrawable": "0.0",
            "crossMaintenanceMarginUsed": "0.0", "assetPositions": [], "time": 1791700000000}

    def responder(path, payload):
        kind = payload["type"]
        if kind == "userAbstraction":
            return mode() if callable(mode) else mode
        if kind == "spotClearinghouseState":
            return spot
        if kind == "clearinghouseState":
            return (xyz or flat) if payload["dex"] == "xyz" else flat
        raise AssertionError(payload)

    return responder


async def test_a_unified_account_reads_its_one_balance_from_spot_usdc():
    """Per-dex account values are zero in this mode; the collateral is the spot USDC balance."""
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "9.79", "hold": "0.0", "entryNtl": "0.0"}, {"coin": "USDT0", "token": 268, "total": "0.0", "hold": "0.0"}],
            "tokenToAvailableAfterMaintenance": [[0, "9.79"]]}
    adapter = Stub(_unified_responder(spot), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32))
    account = await adapter.get_account()
    assert account.unified is True and set(account.by_dex) == {"", "xyz"}
    for dex in ("", "xyz"):  # every managed market finds the same balance
        acct = account.dex_account(dex)
        assert acct.account_value == 9.79 and acct.available_margin == pytest.approx(9.79) and acct.withdrawable == 9.79 and acct.margin_used == 0.0
    once = account.accounts_once()
    assert list(once) == [""] and once[""].account_value == 9.79  # but it is one balance: counted once
    await adapter.stop()


async def test_a_unified_balance_is_never_overstated():
    """Held collateral and margin reduce what is free, an unrealised loss reduces equity, an unrealised gain is not counted,
    and the exchange's own 'available after maintenance' caps the result. (No position has been observed on the real
    exchange in this mode yet: these are the conservative rules, not a recording.)"""
    position = {"type": "oneWay", "position": {"coin": COIN, "szi": "0.0081", "entryPx": "4195.0", "unrealizedPnl": "-0.50", "leverage": {"type": "cross", "value": 11},
                                               "liquidationPx": "3900", "marginUsed": "3.09", "cumFunding": {"sinceOpen": "0.0"}}}
    xyz = {"marginSummary": {"accountValue": "0.0", "totalNtlPos": "33.98", "totalMarginUsed": "3.09"}, "withdrawable": "0.0", "crossMaintenanceMarginUsed": "0.7",
           "assetPositions": [position], "time": 1791700000000}
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "9.79", "hold": "3.50"}], "tokenToAvailableAfterMaintenance": [[0, "5.40"]]}
    adapter = Stub(_unified_responder(spot, xyz=xyz), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32))
    acct = (await adapter.get_account()).dex_account("xyz")
    assert acct.account_value == pytest.approx(9.29)                    # 9.79 less the 0.50 unrealised loss
    assert acct.available_margin == pytest.approx(5.40)                 # 9.29 - max(hold 3.50, margin 3.09) = 5.79, capped by the exchange's 5.40
    assert acct.total_notional == pytest.approx(33.98) and acct.maintenance_margin_used == pytest.approx(0.7)
    await adapter.stop()

    position["position"]["unrealizedPnl"] = "2.00"                      # a paper gain is not spendable equity here
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "9.79", "hold": "3.50"}]}
    adapter = Stub(_unified_responder(spot, xyz=xyz), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32))
    acct = (await adapter.get_account()).dex_account("xyz")
    assert acct.account_value == pytest.approx(9.79) and acct.available_margin == pytest.approx(6.29)
    await adapter.stop()

    # no USDC at all, or an answer without balances: zero, which blocks entries (fails closed)
    for empty in ({"balances": []}, {}, {"balances": [{"coin": "USDT0", "token": 268, "total": "50.0", "hold": "0.0"}]}):
        adapter = Stub(_unified_responder(empty), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32))
        account = await adapter.get_account()
        assert account.unified and account.dex_account("xyz").account_value == 0.0 and account.dex_account("xyz").available_margin == 0.0
        await adapter.stop()


async def test_the_account_mode_is_followed_when_it_changes_and_a_failed_lookup_keeps_the_last_one():
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "9.79", "hold": "0.0"}]}
    modes = iter(["unifiedAccount", OutcomeUnknown("ReadTimeout"), "disabled"])
    clock = ManualClock(datetime(2026, 10, 11, tzinfo=UTC))
    adapter = Stub(_unified_responder(spot, mode=lambda: next(modes)), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32), clock=clock)
    assert (await adapter.get_account()).unified is True
    clock.advance(61, monotonic=False)
    assert (await adapter.get_account()).unified is True                # the lookup failed: the last known mode stays
    clock.advance(61, monotonic=False)
    account = await adapter.get_account()
    assert account.unified is False and account.dex_account("xyz").account_value == 0.0  # switched to standard on the exchange: per-dex figures again
    await adapter.stop()
    # never known and the lookup fails: read as standard, where an unfunded dex blocks entries
    adapter = Stub(_unified_responder(spot, mode=lambda: OutcomeUnknown("ReadTimeout")), account_address=ACCOUNT, agent_key="0x" + secrets.token_hex(32))
    assert (await adapter.get_account()).unified is False
    await adapter.stop()


async def test_fills_are_paged_by_time_deduplicated_by_tid_and_limited_to_managed_coins():
    pages = []

    def responder(path, payload):
        pages.append(payload["startTime"])
        if payload["startTime"] == 100:
            return [{**FILL, "tid": i, "time": 100 + i} for i in range(2000)]
        return [{**FILL, "tid": 1999, "time": 2099}, {**FILL, "tid": 5000, "time": 2100}, {**FILL, "tid": 5001, "coin": "BTC", "time": 2101}]

    adapter = Stub(responder, account_address=ACCOUNT)
    fills = await adapter.get_fills(100)
    assert pages == [100, 2099] and len(fills) == 2001 and fills[-1].tid == 5000
    assert len({f.tid for f in fills}) == len(fills) and all(f.coin == COIN for f in fills)
    await adapter.stop()


async def test_reads_without_an_account_fail_closed():
    adapter = Stub(info_responder)
    with pytest.raises(ExchangeError) as info:
        await adapter.get_account()
    assert info.value.kind == ErrorClass.AUTH
    await adapter.stop()


# ---------------------------------------------------------------- signer verification (FR-070)
async def test_signer_must_be_an_agent_of_the_configured_account():
    key = "0x" + secrets.token_hex(32)
    state = {"role": {"role": "agent", "data": {"user": ACCOUNT}}, "agents": []}
    adapter = Stub(lambda path, payload: state["role"] if payload["type"] == "userRole" else state["agents"], account_address=ACCOUNT, agent_key=key)
    signer = adapter._wallet.address.lower()
    assert (await adapter.verify_signer()).authorized
    state["agents"] = [{"name": "bot", "address": signer.upper().replace("0X", "0x"), "validUntil": 1800000000000}]
    assert (await adapter.verify_signer()).expires_at_ms == 1800000000000
    state["role"] = {"role": "agent", "data": {"user": "0x" + "99" * 20}}
    assert not (await adapter.verify_signer()).authorized
    state["role"] = {"role": "missing"}
    assert not (await adapter.verify_signer()).authorized
    await adapter.stop()


async def test_the_master_key_is_refused_as_a_signer_and_no_key_means_read_only():
    """SR-01: trade with an agent key only."""
    from eth_account import Account

    key = "0x" + secrets.token_hex(32)
    master = Stub(lambda *a: {"role": "user"}, account_address=Account.from_key(key).address, agent_key=key)
    info = await master.verify_signer()
    assert not info.authorized and "master key" in info.detail and not master.calls
    read_only = Stub(lambda *a: {})
    assert not (await read_only.verify_signer()).authorized
    with pytest.raises(ExchangeError) as error:
        read_only.sign({"type": "order"}, 1, None)
    assert error.value.kind == ErrorClass.AUTH
    await master.stop()
    await read_only.stop()


# ---------------------------------------------------------------- signed actions (§13.2)
async def signing_adapter(exchange_response):
    key = "0x" + secrets.token_hex(32)

    def responder(path, payload):
        if path == "/info":
            return info_responder(path, payload)
        return exchange_response(payload) if callable(exchange_response) else exchange_response

    adapter = Stub(responder, account_address=ACCOUNT, agent_key=key)
    await adapter.get_instruments()
    adapter.calls.clear()
    return adapter


async def test_entry_request_matches_the_documented_shape_and_is_signed_by_the_agent():
    adapter = await signing_adapter({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.05", "avgPx": "4287.0", "oid": 9}}]}}})
    cloid = make_cloid("testnet", ACCOUNT, "-100:1", "ENTRY", 0, 1)
    nonce, expires = 1791652503470, 1791652508470
    results = await adapter.place([OrderRequest(COIN, False, 0.05, 4284.0, False, cloid, "limit", "Ioc")], nonce, expires)
    assert results[0].status == "filled"
    path, payload = adapter.calls[0]
    assert path == "/exchange"
    assert payload["action"] == {"type": "order", "grouping": "na", "orders": [
        {"a": 110003, "b": False, "p": "4284", "s": "0.05", "r": False, "t": {"limit": {"tif": "Ioc"}}, "c": cloid}]}
    assert payload["nonce"] == nonce and payload["expiresAfter"] == expires and payload["vaultAddress"] is None
    signer = recover_agent_or_user_from_l1_action(payload["action"], payload["signature"], None, nonce, expires, False)
    assert signer.lower() == adapter._wallet.address.lower()
    assert adapter._wallet.key.hex() not in json.dumps(payload)  # the key itself never travels
    await adapter.stop()


async def test_stop_and_take_profit_request_shapes():
    adapter = await signing_adapter({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}, {"resting": {"oid": 2}}]}}})
    sl = OrderRequest(COIN, True, 0.05, 4725.6, True, "0x" + "a" * 32, "trigger", trigger_px=4296.0, tpsl="sl")
    tp = OrderRequest(COIN, True, 0.01, 4283.0, True, "0x" + "b" * 32, "limit", "Gtc")
    await adapter.place([sl, tp], 5)
    orders = adapter.calls[0][1]["action"]["orders"]
    assert orders[0] == {"a": 110003, "b": True, "p": "4725.6", "s": "0.05", "r": True, "c": "0x" + "a" * 32,
                         "t": {"trigger": {"isMarket": True, "triggerPx": "4296", "tpsl": "sl"}}}
    assert orders[1] == {"a": 110003, "b": True, "p": "4283", "s": "0.01", "r": True, "t": {"limit": {"tif": "Gtc"}}, "c": "0x" + "b" * 32}
    assert adapter.calls[0][1]["expiresAfter"] is None
    await adapter.stop()


async def test_cancel_modify_and_leverage_actions():
    def exchange(payload):
        kind = payload["action"]["type"]
        if kind == "cancelByCloid":
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success", {"error": "Order was never placed, already canceled, or filled."}]}}}
        if kind == "batchModify":
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 77}}]}}}
        return {"status": "ok", "response": {"type": "default"}}

    adapter = await signing_adapter(exchange)
    results = await adapter.cancel(COIN, ["0x" + "a" * 32, "0x" + "b" * 32], 1)
    assert results[0] is None and results[1].kind == ErrorClass.NOT_FOUND
    assert adapter.calls[0][1]["action"] == {"type": "cancelByCloid", "cancels": [{"asset": 110003, "cloid": "0x" + "a" * 32}, {"asset": 110003, "cloid": "0x" + "b" * 32}]}
    modified = await adapter.modify("0x" + "a" * 32, OrderRequest(COIN, True, 0.04, 4725.6, True, "0x" + "a" * 32, "trigger", trigger_px=4296.0, tpsl="sl"), 2)
    assert modified.status == "resting" and modified.oid == 77
    action = adapter.calls[1][1]["action"]
    assert action["type"] == "batchModify" and action["modifies"][0]["oid"] == "0x" + "a" * 32 and action["modifies"][0]["order"]["s"] == "0.04"
    await adapter.set_leverage(COIN, 5, True, 3)
    assert adapter.calls[2][1]["action"] == {"type": "updateLeverage", "asset": 110003, "isCross": True, "leverage": 5}
    await adapter.stop()


async def test_sub_account_trading_sets_the_vault_address_and_queries_it():
    vault = "0x" + "5e" * 20
    key = "0x" + secrets.token_hex(32)
    seen = {}

    def responder(path, payload):
        if path == "/info" and payload["type"] in ("perpDexs", "meta"):
            return info_responder(path, payload)
        seen[path] = payload
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}} if path == "/exchange" else []

    adapter = Stub(responder, account_address=ACCOUNT, vault_address=vault, agent_key=key)
    await adapter.get_instruments()
    await adapter.get_open_orders()
    assert seen["/info"]["user"] == vault
    await adapter.place([OrderRequest(COIN, True, 0.01, 4283.0, True, "0x" + "b" * 32)], 9)
    payload = seen["/exchange"]
    assert payload["vaultAddress"] == vault
    assert recover_agent_or_user_from_l1_action(payload["action"], payload["signature"], vault, 9, None, False).lower() == adapter._wallet.address.lower()
    await adapter.stop()


async def test_exchange_error_status_is_classified_and_never_mistaken_for_success():
    adapter = await signing_adapter({"status": "err", "response": "User or API Wallet 0xabc does not exist."})
    with pytest.raises(ExchangeError) as info:
        await adapter.place([OrderRequest(COIN, True, 0.01, 4283.0, True, "0x" + "b" * 32)], 1)
    assert info.value.kind == ErrorClass.AUTH
    adapter._responder = lambda path, payload: "gateway timeout page"
    with pytest.raises(OutcomeUnknown):
        await adapter.place([OrderRequest(COIN, True, 0.01, 4283.0, True, "0x" + "c" * 32)], 2)
    await adapter.stop()


def test_http_failures_map_to_rate_limit_or_unknown_outcome():
    import requests

    class Raising(HyperliquidAdapter):
        def __init__(self, exc):
            super().__init__(rest_url="https://example.invalid", ws_url="ws://x", is_mainnet=False, coins=[COIN])
            self.exc = exc

        def _api(self):
            exc = self.exc

            class Api:
                def post(self, path, payload):
                    raise exc

            return Api()

    with pytest.raises(ExchangeError) as info:
        Raising(ClientError(429, None, "rate limited", None, None))._post_sync("/info", {})
    assert info.value.kind == ErrorClass.RATE_LIMIT
    with pytest.raises(ExchangeError) as info:
        Raising(ClientError(422, "x", "Insufficient margin to place order.", None, None))._post_sync("/exchange", {})
    assert info.value.kind == ErrorClass.INSUFFICIENT_MARGIN
    for exc in (ServerError(502, "bad gateway"), requests.Timeout(), requests.ConnectionError()):
        with pytest.raises(OutcomeUnknown):  # 5xx / timeout / connection error: never assume it failed
            Raising(exc)._post_sync("/exchange", {})


async def test_an_unresolved_asset_is_never_sent():
    key = "0x" + secrets.token_hex(32)
    adapter = Stub(lambda *a: {}, account_address=ACCOUNT, agent_key=key)
    with pytest.raises(ExchangeError) as info:
        await adapter.place([OrderRequest(COIN, True, 0.01, 4283.0, True, "0x" + "b" * 32)], 1)
    assert info.value.kind == ErrorClass.LOCAL_BUG and not adapter.calls
    await adapter.stop()


# ---------------------------------------------------------------- stream dispatch
async def test_stream_messages_are_dispatched_to_typed_handlers_and_filtered_to_managed_coins():
    from hlbot.exchange.base import StreamHandlers

    adapter = Stub(lambda *a: {}, account_address=ACCOUNT)
    got = {"quote": [], "ctx": [], "fill": [], "update": [], "liq": [], "funding": []}

    async def on_fill(fill, snapshot):
        got["fill"].append((fill.tid, snapshot))

    adapter._handlers = StreamHandlers(
        on_quote=lambda q: _append(got["quote"], q), on_context=lambda c: _append(got["ctx"], c), on_fill=on_fill,
        on_order_update=lambda u: _append(got["update"], u), on_liquidation=lambda e: _append(got["liq"], e),
        on_funding=lambda f: _append(got["funding"], f),
    )
    await adapter._on_ws_message({"channel": "bbo", "data": {"coin": COIN, "time": 5, "bbo": [{"px": "4191.4", "sz": "1.5", "n": 2}, None]}})
    await adapter._on_ws_message({"channel": "activeAssetCtx", "data": {"coin": COIN, "ctx": {"markPx": "4191.5", "oraclePx": "4191.3", "funding": "0.00000625"}}})
    await adapter._on_ws_message({"channel": "userFills", "data": {"isSnapshot": True, "user": ACCOUNT, "fills": [FILL, {**FILL, "tid": 78, "coin": "BTC"}]}})
    await adapter._on_ws_message({"channel": "orderUpdates", "data": [{"order": {"coin": COIN, "oid": 5002, "cloid": "0x" + "d" * 32, "sz": "0.0", "origSz": "0.2"}, "status": "filled", "statusTimestamp": 9}]})
    await adapter._on_ws_message({"channel": "user", "data": {"liquidation": {"lid": 1, "liquidated_user": ACCOUNT}}})
    await adapter._on_ws_message({"channel": "user", "data": {"funding": {"time": 10, "coin": COIN, "usdc": "-0.02", "szi": "-0.24", "fundingRate": "0.0000125"}}})
    await adapter._on_ws_message({"channel": "error", "data": "bad subscription"})
    assert got["quote"][0].bid == 4191.4 and got["quote"][0].ask is None  # a one-sided book is reported as it is
    assert got["ctx"][0].mark == 4191.5 and got["ctx"][0].oracle == 4191.3
    assert got["fill"] == [(77, True)] and got["update"][0].status == "filled" and got["update"][0].cloid == "0x" + "d" * 32
    assert len(got["liq"]) == 1 and got["funding"][0].usdc == -0.02
    await adapter.stop()


async def _append(target, value):
    target.append(value)


# ---------------------------------------------------------------- WebSocket lifecycle (§9.6.5)
class WsServer:
    def __init__(self):
        self.received = []
        self.sockets = []
        self.connections = 0
        self.answer_pings = True

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self.sockets.append(ws)
        async for message in ws:
            data = json.loads(message.data)
            self.received.append(data)
            if data.get("method") == "ping" and self.answer_pings:
                await ws.send_json({"channel": "pong"})
            if data.get("method") == "subscribe":
                await ws.send_json({"channel": "subscriptionResponse", "data": data})
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/ws"

    async def stop(self):
        await self.runner.cleanup()


async def wait_for(predicate, timeout=5.0):  # noqa: ASYNC109
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_ws_subscribes_pings_dispatches_and_resubscribes_after_an_unannounced_disconnect(monkeypatch):
    import hlbot.exchange.hl_ws as module

    monkeypatch.setattr(module, "MIN_RECONNECT_GAP_S", 0.05)
    monkeypatch.setattr(module, "BACKOFF_START_S", 0.05)
    server = WsServer()
    url = await server.start()
    health = StreamHealth()
    messages, connections = [], []
    subscriptions = [{"type": "bbo", "coin": COIN}, {"type": "userFills", "user": ACCOUNT}]

    async def on_message(message):
        messages.append(message)

    async def on_connection(connected):
        connections.append(connected)

    ws = HyperliquidWs(url, subscriptions, on_message=on_message, on_connection=on_connection, health=health, stale_ms=500)
    assert ws.ping_interval_s == 1.0  # short enough that a quiet market still answers inside the stale window
    ws.ping_interval_s = 0.1
    task = asyncio.ensure_future(ws.run())
    await wait_for(lambda: len([m for m in server.received if m.get("method") == "subscribe"]) == 2)
    assert [m["subscription"] for m in server.received if m.get("method") == "subscribe"] == subscriptions
    await wait_for(lambda: any(m.get("method") == "ping" for m in server.received))  # heartbeat: {"method": "ping"}
    await server.sockets[0].send_json({"channel": "bbo", "data": {"coin": COIN}})
    await wait_for(lambda: messages == [{"channel": "bbo", "data": {"coin": COIN}}])  # pong / subscription acks are not forwarded
    assert health.connected and connections == [True]
    assert health.ping_ms is not None and 0 <= health.ping_ms < 2000  # ping -> pong round trip is measured

    await server.sockets[0].close()  # "disconnects may happen periodically and without announcement"
    await wait_for(lambda: server.connections == 2 and len([m for m in server.received if m.get("method") == "subscribe"]) == 4)
    await wait_for(lambda: connections == [True, False, True])
    assert health.reconnects == 1 and health.connected

    await ws.stop()
    await asyncio.wait_for(task, 3)
    assert not health.connected
    await server.stop()


async def test_ws_watchdog_reconnects_a_silent_connection(monkeypatch):
    import hlbot.exchange.hl_ws as module

    monkeypatch.setattr(module, "MIN_RECONNECT_GAP_S", 0.05)
    monkeypatch.setattr(module, "BACKOFF_START_S", 0.05)
    server = WsServer()
    server.answer_pings = False
    url = await server.start()
    health = StreamHealth()

    async def nothing(*args):
        return None

    ws = HyperliquidWs(url, [], on_message=nothing, on_connection=nothing, health=health, stale_ms=100)
    ws.ping_interval_s = 0.05
    task = asyncio.ensure_future(ws.run())
    await wait_for(lambda: server.connections >= 2, timeout=6)  # no pong, no data: the client gives up and reconnects
    await ws.stop()
    await asyncio.wait_for(task, 3)
    await server.stop()


def test_backoff_is_capped_at_thirty_seconds():
    delay = 1.0
    for _ in range(10):
        delay = next_backoff(delay)
    assert delay == 30.0 and next_backoff(0.1) == 1.0


async def test_rest_latency_is_recorded_for_failures_too_and_averaged_over_recent_calls():
    import time as real_time

    def slow_then_fail(path, payload):
        real_time.sleep(0.03)
        if payload["type"] == "l2Book":
            return OutcomeUnknown("timeout")
        return info_responder(path, payload)

    adapter = Stub(slow_then_fail)
    await adapter.get_instruments()
    with pytest.raises(OutcomeUnknown):
        await adapter.get_book(COIN)
    health = adapter.stream_health()
    assert health.rest_ms >= 25 and health.rest_avg_ms >= 25 and len(adapter._latencies) == 4
    for _ in range(30):
        await adapter._info({"type": "perpDexs"})
    assert len(adapter._latencies) == 20  # a rolling window, not the whole history
    await adapter.stop()
