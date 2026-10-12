"""Sizing and every risk control (FR-030..FR-042, TC-035, TC-100, TC-102, TC-103)."""

import dataclasses

import pytest

from hlbot.domain.risk import (
    AccountView,
    Candidate,
    InstrumentSpec,
    MarketView,
    RiskConfig,
    StateView,
    assess,
    build_ladder,
    compute_size,
    daily_loss_reached,
    daily_target_reached,
    estimate_liquidation_price,
    loss_per_unit,
    risk_reward,
    sizing_equity,
)

TPS = [
    {"level": 1, "price": 4283.0, "close_percent": 20.0},
    {"level": 2, "price": 4280.0, "close_percent": 20.0},
    {"level": 3, "price": 4277.0, "close_percent": 20.0},
    {"level": 4, "price": 4274.0, "close_percent": 20.0},
    {"level": 5, "price": 4270.0, "close_percent": 20.0},
]
INST = InstrumentSpec(sz_decimals=4, max_leverage=25)
CFG = RiskConfig()


def candidate(**over):
    base = dict(coin="xyz:GOLD", direction="SELL", ref_price=4287.5, limit_price=4287.5, sl=4296.0, tp_levels=TPS)
    return Candidate(**{**base, **over})


def account(**over):
    base = dict(equity=1000.0, available_margin=1000.0, total_notional=0.0, maintenance_margin_used=0.0, age_ms=100.0)
    return AccountView(**{**base, **over})


def market(**over):
    return MarketView(**{**dict(mark=4287.5, oracle=4287.4, quote_age_ms=50.0), **over})


def state(**over):
    return StateView(**{**dict(open_trades_total=0, open_trades_on_asset=0, day_pnl=0.0, day_start_equity=1000.0, fee_rate=0.00045), **over})


def run(cfg=CFG, **parts):
    return assess(
        parts.get("candidate", candidate()), parts.get("instrument", INST), parts.get("account", account()),
        parts.get("market", market()), parts.get("state", state()), cfg,
    )


# ---------------------------------------------------------------- formulas


def test_fr030_worked_example():
    """Equity 10,000, risk 0.5 %, worst-case distance 10.0 -> size 5.0000."""
    assert compute_size(10000.0, 0.5, 100.0, 91.0, 1.0, 0.0, 4) == 5.0


def test_size_never_rounds_up():
    size = compute_size(1000.0, 0.5, 4287.5, 4296.0, 1.0, 0.00045, 4)
    distance = loss_per_unit(4287.5, 4296.0, 1.0, 0.00045)
    assert size * distance <= 5.0
    assert (size + 0.0001) * distance > 5.0


def test_loss_per_unit_includes_allowance_and_both_fees():
    assert loss_per_unit(4284.0, 4296.0, 1.0, 0.001) == pytest.approx(12.0 + 1.0 + 0.001 * (4284.0 + 4296.0))


def test_tc103_risk_reward_worked_example():
    """PRD §10.3: 1.26 at the zone midpoint, 0.60 at the edge of the buffered range."""
    weighted = [(leg["price"], 0.2) for leg in TPS]
    assert round(risk_reward(4287.5, 4296.0, weighted), 2) == 1.26
    assert round(risk_reward(4284.0, 4296.0, weighted), 2) == 0.60
    assert sum(w * abs(p - 4287.5) for p, w in weighted) == pytest.approx(10.7)
    assert sum(w * abs(p - 4284.0) for p, w in weighted) == pytest.approx(7.2)


def test_ported_sizing_equity():
    assert sizing_equity(True, 5000.0, 3000.0) == 5000.0
    assert sizing_equity(False, 5000.0, 3000.0) == 3000.0
    assert sizing_equity(False, 5000.0, None) == 0.0


def test_ported_daily_target():
    assert not daily_target_reached(1000.0, 10000.0, None)
    assert daily_target_reached(200.0, 10000.0, 2.0)
    assert not daily_target_reached(199.99, 10000.0, 2.0)
    assert daily_target_reached(250.0, 10000.0, 2.0)


def test_daily_loss_boundary():
    assert not daily_loss_reached(-19.99, 1000.0, 2.0)
    assert daily_loss_reached(-20.0, 1000.0, 2.0)
    assert daily_loss_reached(-20.1, 1000.0, 2.0)
    assert not daily_loss_reached(-500.0, 1000.0, None)


def test_liquidation_estimate_direction_and_no_liquidation_case():
    short = estimate_liquidation_price("SELL", 4284.0, 1.0, 25, equity=500.0)
    assert short > 4284.0
    long_ = estimate_liquidation_price("BUY", 4284.0, 1.0, 25, equity=500.0)
    assert long_ < 4284.0
    assert estimate_liquidation_price("BUY", 4284.0, 0.01, 25, equity=10000.0) is None
    isolated = estimate_liquidation_price("SELL", 4284.0, 1.0, 25, equity=0.0, isolated_leverage=5)
    assert isolated == pytest.approx(4284.0 + (4284.0 / 5 - 4284.0 / 50) / 1.02)


def test_ladder_rounds_prices_toward_entry_and_sums_to_size():
    levels = [{"level": 1, "price": 4282.93, "close_percent": 50.0}, {"level": 2, "price": 4279.96, "close_percent": 50.0}]
    legs = build_ladder("SELL", 0.2413, levels, 4, 10.0)
    assert [leg["price"] for leg in legs] == [4283.0, 4280.0]
    assert sum(leg["size"] for leg in legs) == pytest.approx(0.2413)
    assert build_ladder("SELL", 0.002, levels, 4, 10.0) is None


# ---------------------------------------------------------------- assess: approval


def test_valid_candidate_is_approved_with_full_ladder():
    result = run()
    assert result.approved, result.violations
    assert result.size == pytest.approx(0.3741)  # floor(5.00 / 13.3626, 4 dp)
    assert len(result.legs) == 5 and sum(leg["size"] for leg in result.legs) == pytest.approx(result.size)
    assert result.risk_pct <= CFG.risk_per_trade_pct
    assert result.rr == pytest.approx(1.26, abs=0.01)
    assert result.sl_price == 4296.0


def test_every_violation_is_listed_not_just_the_first():
    result = run(cfg=dataclasses.replace(CFG, min_rr=5.0, max_sl_distance=1.0, max_trade_notional=10.0))
    assert not result.approved
    assert {"MIN_RR", "MAX_SL_DISTANCE", "MAX_NOTIONAL"} <= set(result.rules())
    assert not result.transient_only


# ---------------------------------------------------------------- TC-100: each control at its limit and just beyond


def test_rc01_max_risk_per_trade():
    """FR-032: 0.51 % against a 0.50 % limit is rejected. Reachable when
    sizing uses a fixed baseline larger than live equity."""
    cfg = dataclasses.replace(CFG, compound_enabled=False, fixed_equity_baseline=1000.0)
    assert run(cfg=cfg, account=account(equity=1000.0)).approved
    over = run(cfg=cfg, account=account(equity=975.0))
    assert "MAX_RISK" in over.rules() and over.risk_pct > 0.5


def test_rc02_daily_loss():
    assert run(state=state(day_pnl=-19.9)).approved
    assert "DAILY_LOSS" in run(state=state(day_pnl=-20.1)).rules()


def test_rc03_daily_target():
    cfg = dataclasses.replace(CFG, daily_target_pct=1.0)
    assert run(cfg=cfg, state=state(day_pnl=9.99)).approved
    assert "DAILY_TARGET" in run(cfg=cfg, state=state(day_pnl=10.0)).rules()


def test_rc04_open_trades():
    assert "POSITION_OPEN" in run(state=state(open_trades_on_asset=1, open_trades_total=1)).rules()
    assert run(state=state(open_trades_total=2)).approved
    assert "MAX_OPEN_TRADES" in run(state=state(open_trades_total=3)).rules()


def test_rc05_max_exposure_fr036_example():
    """Limit 3.0x, equity 1,000, existing 2,500, new 600 -> rejected."""
    cfg = dataclasses.replace(CFG, sizing_mode="fixed", fixed_size=0.14, risk_per_trade_pct=5.0, max_trade_notional=None)
    result = run(cfg=cfg, account=account(total_notional=2500.0))
    assert result.notional == pytest.approx(600.25)
    assert "MAX_EXPOSURE" in result.rules()
    assert "MAX_EXPOSURE" not in run(cfg=cfg, account=account(total_notional=2399.0)).rules()


def test_rc06_leverage_cannot_exceed_asset_maximum():
    assert "LEVERAGE" in run(cfg=dataclasses.replace(CFG, leverage=26)).rules()
    assert run(cfg=dataclasses.replace(CFG, leverage=25)).approved


def test_rc07_min_rr():
    edge = candidate(ref_price=4284.0, limit_price=4284.0)
    assert run(candidate=edge, market=market(mark=4284.0, oracle=4284.0)).approved  # 0.60 >= 0.5
    assert "MIN_RR" in run(cfg=dataclasses.replace(CFG, min_rr=1.0), candidate=edge, market=market(mark=4284.0, oracle=4284.0)).rules()


def test_rc10_max_sl_distance():
    assert run(cfg=dataclasses.replace(CFG, max_sl_distance=8.5)).approved
    assert "MAX_SL_DISTANCE" in run(cfg=dataclasses.replace(CFG, max_sl_distance=8.4)).rules()


def test_rc11_min_notional_and_size_zero():
    tiny = run(account=account(equity=5.0, available_margin=5.0))  # 0.0018 units = about 8 USDC
    assert not tiny.approved and "MIN_NOTIONAL" in tiny.rules()
    assert "SIZE_ZERO" in run(account=account(equity=0.001, available_margin=0.001)).rules()


def test_rc12_margin_usage_cap_tc035():
    cfg = dataclasses.replace(CFG, leverage=2)
    result = run(cfg=cfg)
    assert result.margin_required == pytest.approx(result.notional / 2)
    assert "MARGIN" in run(cfg=cfg, account=account(available_margin=result.margin_required * 2 - 1)).rules()
    assert "MARGIN" not in run(cfg=cfg, account=account(available_margin=result.margin_required * 2 + 1)).rules()


def test_rc13_liquidation_buffer_fr041_example():
    """Liquidation distance 12 with SL distance 10 and buffer 1.5 -> rejected."""
    cfg = dataclasses.replace(CFG, sizing_mode="fixed", fixed_size=1.0, risk_per_trade_pct=5.0, max_trade_notional=None,
                              max_exposure_x_equity=50.0, margin_usage_cap=0.9, leverage=25)
    cand = candidate(ref_price=4286.0, limit_price=4286.0, sl=4296.0)
    maint = 4286.0 / 50.0
    equity_for = lambda distance: maint + distance * 1.02  # noqa: E731 - inverts the liquidation formula for a short
    near = run(cfg=cfg, candidate=cand, account=account(equity=equity_for(12.0), available_margin=10000.0))
    assert near.liq_distance == pytest.approx(12.0)
    assert "LIQUIDATION_DISTANCE" in near.rules()
    far = run(cfg=cfg, candidate=cand, account=account(equity=equity_for(15.5), available_margin=10000.0))
    assert "LIQUIDATION_DISTANCE" not in far.rules()


def test_rc16_basis_guard_fr025_example():
    """Guard 3.0 points, observed deviation 4.0 -> BASIS_EXCEEDED."""
    cfg = dataclasses.replace(CFG, max_mark_oracle_dev_points=3.0)
    assert run(cfg=cfg, market=market(mark=4287.5, oracle=4284.5)).approved
    assert "BASIS_EXCEEDED" in run(cfg=cfg, market=market(mark=4287.5, oracle=4283.5)).rules()


def test_rc19_max_notional_per_trade():
    result = run(account=account(equity=10000.0, available_margin=10000.0))
    assert result.notional > 5000 and "MAX_NOTIONAL" in result.rules()
    assert run(cfg=dataclasses.replace(CFG, max_trade_notional=result.notional + 1), account=account(equity=10000.0, available_margin=10000.0)).approved


def test_fr031_fixed_size_above_a_limit_is_rejected_not_reduced():
    cfg = dataclasses.replace(CFG, sizing_mode="fixed", fixed_size=5.0)
    result = run(cfg=cfg)
    assert not result.approved and result.size == 5.0
    assert {"MAX_RISK", "MAX_NOTIONAL"} <= set(result.rules())


# ---------------------------------------------------------------- FR-042 / RC-15: fail closed


def test_tc102_stale_account_fails_closed_and_waits():
    result = run(account=account(age_ms=5001.0))
    assert not result.approved and result.rules() == ["ACCOUNT_STALE"] and result.transient_only
    assert run(account=account(age_ms=5000.0)).approved


def test_fr024_stale_quote_waits():
    result = run(market=market(quote_age_ms=3000.0))
    assert result.rules() == ["DATA_STALE"] and result.transient_only
    assert run(market=market(quote_age_ms=2000.0)).approved


@pytest.mark.parametrize(
    "parts, rule",
    [
        ({"account": None}, "ACCOUNT_UNVERIFIABLE"),
        ({"account": account(equity=None)}, "ACCOUNT_UNVERIFIABLE"),
        ({"market": None}, "DATA_STALE"),
        ({"state": state(fee_rate=None)}, "FEE_UNVERIFIABLE"),
        ({"state": state(open_trades_total=None)}, "STATE_UNVERIFIABLE"),
        ({"state": state(day_pnl=None)}, "DAY_STATE_UNVERIFIABLE"),
    ],
)
def test_any_unreadable_input_fails_closed(parts, rule):
    result = run(**parts)
    assert not result.approved and rule in result.rules() and result.transient_only and result.size == 0.0


def test_sl_already_through_the_market_waits():
    result = run(candidate=candidate(ref_price=4296.5, limit_price=4294.0))
    assert result.rules() == ["SL_THROUGH_MARKET"] and result.transient_only


def test_buy_side_mirror_is_approved():
    tps = [{"level": i + 1, "price": p, "close_percent": 20.0} for i, p in enumerate([4320.0, 4323.0, 4326.0, 4329.0, 4333.0])]
    buy = candidate(direction="BUY", ref_price=4315.0, limit_price=4315.0, sl=4306.0, tp_levels=tps)
    result = run(candidate=buy, market=market(mark=4315.0, oracle=4315.0))
    assert result.approved, result.violations
    assert all(leg["price"] > 4315.0 for leg in result.legs)


def test_an_unfunded_account_is_neither_a_daily_loss_nor_a_daily_target():
    """Found on the first testnet start: zero equity on the traded dex was
    reported as "daily loss limit reached"."""
    assert not daily_loss_reached(0.0, 0.0, 2.0)
    assert not daily_loss_reached(-5.0, 0.0, 2.0)
    assert not daily_target_reached(0.0, 0.0, 1.0)
    assert not daily_target_reached(3.0, 0.0, 1.0)
    unfunded = run(account=account(equity=0.0, available_margin=0.0), state=state(day_start_equity=0.0))
    assert not unfunded.approved and "DAILY_LOSS" not in unfunded.rules() and "SIZE_ZERO" in unfunded.rules()
