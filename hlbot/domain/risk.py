"""Position sizing and every pre-trade risk control (PRD §10.2, §10.3,
§10.6; FR-030 .. FR-042).

``assess()`` is pure: no I/O, no clock, no SDK. The caller reads the
live figures and passes them in; anything it could not read arrives as
``None`` and fails closed (FR-042). A failed check rejects the trade:
the size is never silently reduced to make it fit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Optional

from hlbot.domain.planning import split_size_units
from hlbot.domain.rounding import (
    MIN_ORDER_NOTIONAL,
    min_size_units,
    round_toward_entry,
    size_units,
    units_to_size,
)

_EPS = 1e-9


# ---------------------------------------------------------------- ported helpers


def sizing_equity(compound_enabled: bool, live_equity: float, fixed_baseline: Optional[float]) -> float:
    """Equity that sizing is based on: live equity when compounding, a
    flat configured baseline otherwise (EX-16)."""
    return live_equity if compound_enabled else (fixed_baseline or 0.0)


def daily_target_reached(day_pnl: float, day_start_equity: float, target_percent: Optional[float]) -> bool:
    """True once the day's P/L has reached ``target_percent`` % of the
    day's starting equity. ``None`` (unset) is never reached."""
    if target_percent is None or day_start_equity <= 0:
        return False  # a percentage of nothing is not a target
    return day_pnl >= day_start_equity * target_percent / 100.0 - _EPS


def daily_loss_reached(day_pnl: float, day_start_equity: float, limit_percent: Optional[float]) -> bool:
    """True once the day's P/L is at or below ``-limit_percent`` % of the
    day's starting equity (FR-033)."""
    if limit_percent is None or day_start_equity <= 0:
        return False  # no equity is a funding problem, reported as such, not a loss
    return day_pnl <= -(day_start_equity * limit_percent / 100.0) + _EPS


# ---------------------------------------------------------------- formulas


def loss_per_unit(limit_price: float, sl: float, sl_slippage_allowance: float, fee_rate: float) -> float:
    """Worst-case loss per unit, both fees included (§10.2):
    ``D = |P_lim - SL| + a + f x (P_lim + SL)``."""
    return abs(limit_price - sl) + sl_slippage_allowance + fee_rate * (limit_price + sl)


def compute_size(
    equity: float,
    risk_percent: float,
    limit_price: float,
    sl: float,
    sl_slippage_allowance: float,
    fee_rate: float,
    sz_decimals: int,
) -> float:
    """``floor((E x r / 100) / D, szDecimals)``; never rounds up (FR-030)."""
    distance = loss_per_unit(limit_price, sl, sl_slippage_allowance, fee_rate)
    if distance <= 0 or equity <= 0 or risk_percent <= 0:
        return 0.0
    return units_to_size(size_units((equity * risk_percent / 100.0) / distance, sz_decimals), sz_decimals)


def risk_reward(limit_price: float, sl: float, weighted_tps: Sequence[tuple]) -> float:
    """``RR = sum(w_i x |TP_i - P_lim|) / |P_lim - SL|`` (§10.3), with
    ``weighted_tps`` as ``(price, weight)`` pairs whose weights sum to 1."""
    risk = abs(limit_price - sl)
    if risk <= 0:
        return 0.0
    return sum(weight * abs(price - limit_price) for price, weight in weighted_tps) / risk


def estimate_liquidation_price(
    direction: str,
    entry_price: float,
    size: float,
    max_leverage: int,
    *,
    equity: float,
    maintenance_margin_used: float = 0.0,
    isolated_leverage: Optional[int] = None,
) -> Optional[float]:
    """Estimated liquidation price of the new position, using the
    exchange's documented formula
    ``liq = P - side x margin_available / size / (1 - l x side)`` with
    ``l = 1 / (2 x maxLeverage)`` (maintenance margin is half the initial
    margin at maximum leverage).

    Cross margin: ``margin_available = equity - maintenance already in
    use - maintenance of this position``. Isolated: the position's own
    margin minus its maintenance. Returns None when the position cannot
    be liquidated by price alone (a long whose estimate is <= 0).
    """
    if size <= 0 or entry_price <= 0 or max_leverage <= 0:
        return entry_price
    side = 1.0 if direction == "BUY" else -1.0
    maint_fraction = 1.0 / (2.0 * max_leverage)
    notional = size * entry_price
    if isolated_leverage:
        margin_available = notional / isolated_leverage - notional * maint_fraction
    else:
        margin_available = equity - maintenance_margin_used - notional * maint_fraction
    if margin_available <= 0:
        return entry_price
    liq = entry_price - side * margin_available / size / (1.0 - maint_fraction * side)
    if side > 0 and liq <= 0:
        return None
    return liq


# ---------------------------------------------------------------- assessment types


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    sizing_mode: str = "risk"  # "risk" | "fixed"
    fixed_size: Optional[float] = None
    compound_enabled: bool = True
    fixed_equity_baseline: Optional[float] = None
    max_daily_loss_pct: Optional[float] = 2.0
    daily_target_pct: Optional[float] = None
    max_open_trades: int = 3
    max_trades_per_asset: int = 1
    max_exposure_x_equity: float = 3.0
    leverage: int = 5
    min_rr: float = 0.5
    sl_slippage_allowance_points: float = 1.0
    max_sl_distance: Optional[float] = 30.0
    margin_usage_cap: float = 0.5
    liq_buffer_x: float = 1.5
    max_mark_oracle_dev_points: Optional[float] = None
    max_trade_notional: Optional[float] = 5000.0
    max_quote_age_ms: int = 2000
    max_account_age_ms: int = 5000
    min_notional: float = MIN_ORDER_NOTIONAL
    margin_mode: str = "cross"  # "cross" | "isolated"


@dataclass(frozen=True)
class Candidate:
    """An entry the engine wants to make right now."""

    coin: str
    direction: str  # "BUY" | "SELL"
    ref_price: float  # best ask (BUY) / best bid (SELL) at decision time
    limit_price: float  # P_lim
    sl: float
    tp_levels: Sequence[dict]  # [{"level", "price", "close_percent"}, ...]


@dataclass(frozen=True)
class InstrumentSpec:
    sz_decimals: int
    max_leverage: int


@dataclass(frozen=True)
class AccountView:
    equity: Optional[float]
    available_margin: Optional[float]
    total_notional: Optional[float]
    maintenance_margin_used: Optional[float] = 0.0
    age_ms: Optional[float] = None


@dataclass(frozen=True)
class MarketView:
    mark: Optional[float]
    oracle: Optional[float]
    quote_age_ms: Optional[float]


@dataclass(frozen=True)
class StateView:
    open_trades_total: Optional[int]
    open_trades_on_asset: Optional[int]
    day_pnl: Optional[float]
    day_start_equity: Optional[float]
    fee_rate: Optional[float]


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str
    transient: bool = False  # True: the signal waits; False: it is rejected


@dataclass
class RiskAssessment:
    approved: bool
    size: float = 0.0
    sl_price: Optional[float] = None
    legs: list[dict] = field(default_factory=list)  # [{"level", "price", "size"}]
    notional: float = 0.0
    loss_per_unit: float = 0.0
    risk_usdc: float = 0.0
    risk_pct: float = 0.0
    rr: float = 0.0
    margin_required: float = 0.0
    est_liq_price: Optional[float] = None
    liq_distance: Optional[float] = None
    sizing_equity: float = 0.0
    violations: list[Violation] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    @property
    def transient_only(self) -> bool:
        """Every violation is transient: wait instead of rejecting."""
        return bool(self.violations) and all(v.transient for v in self.violations)

    def rules(self) -> list[str]:
        return [v.rule for v in self.violations]

    def outputs(self) -> dict:
        return {
            "size": self.size,
            "sl_price": self.sl_price,
            "legs": self.legs,
            "notional": self.notional,
            "loss_per_unit": self.loss_per_unit,
            "risk_usdc": self.risk_usdc,
            "risk_pct": self.risk_pct,
            "rr": self.rr,
            "margin_required": self.margin_required,
            "est_liq_price": self.est_liq_price,
            "liq_distance": self.liq_distance,
            "sizing_equity": self.sizing_equity,
        }


def build_ladder(
    direction: str, size: float, tp_levels: Sequence[dict], sz_decimals: int, min_notional: float
) -> Optional[list[dict]]:
    """TP legs for ``size``: tick-rounded prices (toward entry), sizes
    split by the close percentages, sub-minimum legs merged toward TP1.
    None when ``size`` cannot make one order of at least ``min_notional``."""
    above = direction == "BUY"  # a BUY's targets are above entry
    prices = [round_toward_entry(float(lv["price"]), sz_decimals, level_above_entry=above) for lv in tp_levels]
    minimums = [min_size_units(p, sz_decimals, min_notional) for p in prices]
    split = split_size_units(
        size_units(size, sz_decimals), [float(lv["close_percent"]) for lv in tp_levels], minimums
    )
    if split is None:
        return None
    return [
        {"level": int(lv["level"]), "price": price, "size": units_to_size(units, sz_decimals)}
        for lv, price, units in zip(tp_levels, prices, split, strict=True)
        if units > 0
    ]


def assess(
    candidate: Candidate,
    instrument: InstrumentSpec,
    account: Optional[AccountView],
    market: Optional[MarketView],
    state: StateView,
    cfg: RiskConfig,
) -> RiskAssessment:
    """Size the trade and run every control. Returns an assessment that
    lists *every* violated rule, not just the first."""
    violations: list[Violation] = []
    inputs: dict = {
        "coin": candidate.coin,
        "direction": candidate.direction,
        "ref_price": candidate.ref_price,
        "limit_price": candidate.limit_price,
        "sl": candidate.sl,
        "account": asdict(account) if account else None,
        "market": asdict(market) if market else None,
        "state": asdict(state),
        "leverage": cfg.leverage,
    }
    out = RiskAssessment(approved=False, inputs=inputs)

    def fail(rule: str, detail: str, transient: bool = False) -> None:
        violations.append(Violation(rule, detail, transient))

    # ---- FR-042 / RC-15: anything unreadable or stale fails closed and waits
    if market is None or market.quote_age_ms is None:
        fail("DATA_STALE", "no quote available", transient=True)
    elif market.quote_age_ms > cfg.max_quote_age_ms:
        fail(
            "DATA_STALE",
            f"quote age {market.quote_age_ms:.0f} ms > {cfg.max_quote_age_ms} ms",
            transient=True,
        )

    account_ok = (
        account is not None
        and account.equity is not None
        and account.available_margin is not None
        and account.total_notional is not None
        and account.age_ms is not None
    )
    if not account_ok:
        fail("ACCOUNT_UNVERIFIABLE", "account state could not be read", transient=True)
    elif account.age_ms > cfg.max_account_age_ms:  # type: ignore[union-attr,operator]
        fail(
            "ACCOUNT_STALE",
            f"account state age {account.age_ms:.0f} ms > {cfg.max_account_age_ms} ms",  # type: ignore[union-attr]
            transient=True,
        )

    if state.fee_rate is None:
        fail("FEE_UNVERIFIABLE", "fee rate could not be read", transient=True)
    if state.open_trades_total is None or state.open_trades_on_asset is None:
        fail("STATE_UNVERIFIABLE", "open trade count could not be read", transient=True)
    if (cfg.max_daily_loss_pct is not None or cfg.daily_target_pct is not None) and (
        state.day_pnl is None or state.day_start_equity is None
    ):
        fail("DAY_STATE_UNVERIFIABLE", "day P/L could not be read", transient=True)
    if cfg.max_mark_oracle_dev_points is not None and (
        market is None or market.mark is None or market.oracle is None
    ):
        fail("BASIS_UNVERIFIABLE", "mark/oracle price could not be read", transient=True)

    if violations:
        out.violations = violations
        return out

    assert account is not None and market is not None  # narrowed above
    equity = float(account.equity)  # type: ignore[arg-type]
    fee_rate = float(state.fee_rate)  # type: ignore[arg-type]
    direction = candidate.direction
    limit_price = candidate.limit_price

    # ---- account-level gates
    if state.open_trades_on_asset >= cfg.max_trades_per_asset:  # type: ignore[operator]
        fail("POSITION_OPEN", f"{state.open_trades_on_asset} open trade(s) on {candidate.coin}")
    if state.open_trades_total >= cfg.max_open_trades:  # type: ignore[operator]
        fail("MAX_OPEN_TRADES", f"{state.open_trades_total} open trades >= limit {cfg.max_open_trades}")
    if cfg.max_daily_loss_pct is not None and daily_loss_reached(
        state.day_pnl, state.day_start_equity, cfg.max_daily_loss_pct  # type: ignore[arg-type]
    ):
        fail(
            "DAILY_LOSS",
            f"day P/L {state.day_pnl:.2f} <= -{cfg.max_daily_loss_pct:g}% of {state.day_start_equity:.2f}",
        )
    if daily_target_reached(state.day_pnl or 0.0, state.day_start_equity or 0.0, cfg.daily_target_pct):
        fail(
            "DAILY_TARGET",
            f"day P/L {state.day_pnl:.2f} >= {cfg.daily_target_pct:g}% of {state.day_start_equity:.2f}",
        )
    if cfg.max_mark_oracle_dev_points is not None:
        deviation = abs(market.mark - market.oracle)  # type: ignore[operator]
        if deviation > cfg.max_mark_oracle_dev_points + _EPS:
            fail(
                "BASIS_EXCEEDED",
                f"|mark - oracle| {deviation:g} > {cfg.max_mark_oracle_dev_points:g}",
            )
    if cfg.leverage > instrument.max_leverage:
        fail("LEVERAGE", f"configured leverage {cfg.leverage} > asset maximum {instrument.max_leverage}")

    # ---- levels
    sl_above = direction == "SELL"
    sl_price = round_toward_entry(candidate.sl, instrument.sz_decimals, level_above_entry=sl_above)
    out.sl_price = sl_price
    wrong_side = sl_price <= candidate.ref_price if sl_above else sl_price >= candidate.ref_price
    if wrong_side:
        fail("SL_THROUGH_MARKET", f"SL {sl_price:g} is not beyond price {candidate.ref_price:g}", transient=True)
        out.violations = violations
        return out

    sl_distance = abs(limit_price - sl_price)
    if cfg.max_sl_distance is not None and sl_distance > cfg.max_sl_distance + _EPS:
        fail("MAX_SL_DISTANCE", f"SL distance {sl_distance:g} > {cfg.max_sl_distance:g}")

    # ---- size (§10.2)
    out.loss_per_unit = loss_per_unit(limit_price, sl_price, cfg.sl_slippage_allowance_points, fee_rate)
    out.sizing_equity = sizing_equity(cfg.compound_enabled, equity, cfg.fixed_equity_baseline)
    if cfg.sizing_mode == "fixed":
        size = units_to_size(size_units(cfg.fixed_size or 0.0, instrument.sz_decimals), instrument.sz_decimals)
    else:
        size = compute_size(
            out.sizing_equity,
            cfg.risk_per_trade_pct,
            limit_price,
            sl_price,
            cfg.sl_slippage_allowance_points,
            fee_rate,
            instrument.sz_decimals,
        )
    out.size = size
    if size <= 0:
        fail("SIZE_ZERO", "size rounds down to zero at the configured risk")
        out.violations = violations
        return out

    out.notional = size * limit_price
    out.risk_usdc = size * out.loss_per_unit
    out.risk_pct = out.risk_usdc / equity * 100.0 if equity > 0 else float("inf")
    out.margin_required = out.notional / cfg.leverage

    if out.risk_pct > cfg.risk_per_trade_pct + 1e-6:
        fail("MAX_RISK", f"risk {out.risk_pct:.4f}% of equity > {cfg.risk_per_trade_pct:g}%")
    if out.notional < cfg.min_notional - _EPS:
        fail("MIN_NOTIONAL", f"entry notional {out.notional:.2f} < minimum {cfg.min_notional:g}")
    if cfg.max_trade_notional is not None and out.notional > cfg.max_trade_notional + _EPS:
        fail("MAX_NOTIONAL", f"notional {out.notional:.2f} > {cfg.max_trade_notional:g}")

    available = float(account.available_margin) * cfg.margin_usage_cap  # type: ignore[arg-type]
    if out.margin_required > available + _EPS:
        fail("MARGIN", f"required margin {out.margin_required:.2f} > usable {available:.2f}")

    exposure_cap = min(cfg.max_exposure_x_equity, float(instrument.max_leverage)) * equity
    exposure = float(account.total_notional) + out.notional  # type: ignore[arg-type]
    if exposure > exposure_cap + _EPS:
        fail("MAX_EXPOSURE", f"total notional {exposure:.2f} > {exposure_cap:.2f}")

    # ---- ladder, R:R (§10.3)
    legs = build_ladder(direction, size, candidate.tp_levels, instrument.sz_decimals, cfg.min_notional)
    if legs is None:
        fail("MIN_NOTIONAL", "size cannot make one take-profit order of the minimum value")
        legs = []
    out.legs = legs
    if legs:
        out.rr = risk_reward(limit_price, sl_price, [(leg["price"], leg["size"] / size) for leg in legs])
        if out.rr < cfg.min_rr - 1e-6:
            fail("MIN_RR", f"risk-to-reward {out.rr:.3f} < {cfg.min_rr:g}")

    # ---- liquidation distance (FR-041)
    liq = estimate_liquidation_price(
        direction,
        limit_price,
        size,
        instrument.max_leverage,
        equity=equity,
        maintenance_margin_used=float(account.maintenance_margin_used or 0.0),
        isolated_leverage=cfg.leverage if cfg.margin_mode == "isolated" else None,
    )
    out.est_liq_price = liq
    if liq is not None:
        out.liq_distance = abs(limit_price - liq)
        if out.liq_distance < cfg.liq_buffer_x * sl_distance - _EPS:
            fail(
                "LIQUIDATION_DISTANCE",
                f"liquidation distance {out.liq_distance:.2f} < {cfg.liq_buffer_x:g} x SL distance {sl_distance:g}",
            )

    out.violations = violations
    out.approved = not violations
    return out
