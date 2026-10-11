"""New planning rules: size splitting on size increments and minimum
notional (replaces the baseline's lot-based TestSplitVolume), the entry
limit clamp (TC-013), range edges (TC-006), expiry (TC-005)."""

from datetime import UTC, datetime, timedelta

import pytest

from hlbot.domain.planning import (
    apply_price_offset,
    compute_effective_range,
    compute_expires_at,
    entry_limit_price,
    is_expired,
    is_price_in_range,
    split_size_units,
)


class TestSplitSizeUnits:
    def test_even_split_across_five_levels(self):
        assert split_size_units(500, [20] * 5, [24] * 5) == [100] * 5

    def test_rounding_remainder_goes_to_tp1(self):
        assert split_size_units(7, [20] * 5, [1] * 5) == [3, 1, 1, 1, 1]

    def test_uneven_percentages(self):
        assert split_size_units(500, [50, 30, 20], [24] * 3) == [250, 150, 100]

    def test_sub_minimum_legs_merge_toward_tp1(self):
        # 50 units over five legs is 10 each, below the 24-unit minimum: all of it closes at TP1.
        assert split_size_units(50, [20] * 5, [24] * 5) == [50, 0, 0, 0, 0]

    def test_single_minimum_order_lands_on_tp1(self):
        assert split_size_units(24, [20] * 5, [24] * 5) == [24, 0, 0, 0, 0]

    def test_partial_merge_keeps_far_leg_when_it_can_stand_alone(self):
        assert split_size_units(60, [20] * 5, [24] * 5) == [36, 0, 0, 24, 0]

    def test_below_one_minimum_order_returns_none(self):
        assert split_size_units(20, [20] * 5, [24] * 5) is None
        assert split_size_units(0, [20] * 5, [24] * 5) is None

    @pytest.mark.parametrize("total", [24, 25, 47, 48, 60, 99, 120, 121, 130, 500, 2412])
    def test_legs_always_sum_to_total_and_respect_minimum(self, total):
        legs = split_size_units(total, [20] * 5, [24] * 5)
        assert legs is not None and sum(legs) == total
        assert all(leg == 0 or leg >= 24 for leg in legs)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            split_size_units(100, [50, 50], [1])


class TestEntryLimitPrice:
    """PRD §10.2 and TC-013."""

    def test_sell_uses_slippage_when_far_from_tp1(self):
        assert entry_limit_price("SELL", 4290.0, 3.0, 4283.0, 0.5, 4) == 4287.0

    def test_sell_clamped_against_tp1(self):
        # 4284 - 3 = 4281 would be beyond TP1 4283: clamp to TP1 + gap.
        assert entry_limit_price("SELL", 4284.0, 3.0, 4283.0, 0.5, 4) == 4283.5

    def test_sell_waits_when_clamp_cannot_cross(self):
        assert entry_limit_price("SELL", 4283.2, 3.0, 4283.0, 0.5, 4) is None

    def test_buy_mirror(self):
        assert entry_limit_price("BUY", 4310.0, 3.0, 4320.0, 0.5, 4) == 4313.0
        assert entry_limit_price("BUY", 4318.0, 3.0, 4320.0, 0.5, 4) == 4319.5
        assert entry_limit_price("BUY", 4319.8, 3.0, 4320.0, 0.5, 4) is None

    def test_limit_is_rounded_to_a_valid_tick_in_the_safe_direction(self):
        assert entry_limit_price("BUY", 4310.04, 0.33, 4400.0, 0.5, 4) == 4310.3  # down
        assert entry_limit_price("SELL", 4310.06, 0.33, 4200.0, 0.5, 4) == 4309.8  # up

    def test_zero_slippage_still_crosses_at_the_touch(self):
        assert entry_limit_price("SELL", 4287.0, 0.0, 4283.0, 0.5, 4) == 4287.0


class TestRangeEdges:
    """TC-006: just inside / just outside each edge, both directions use the same range."""

    def test_edges(self):
        effective = compute_effective_range({"low": 4286.0, "high": 4289.0}, 2.0)
        assert effective == {"lower": 4284.0, "upper": 4291.0}
        assert is_price_in_range(4284.0, effective["lower"], effective["upper"])
        assert is_price_in_range(4291.0, effective["lower"], effective["upper"])
        assert not is_price_in_range(4283.9, effective["lower"], effective["upper"])
        assert not is_price_in_range(4291.1, effective["lower"], effective["upper"])


class TestExpiry:
    """TC-005 / FR-020: not entered at or after signal_time + expiry."""

    def test_at_and_after_expiry(self):
        sent = "2026-09-14T10:01:00+00:00"
        expires = compute_expires_at(sent, 30)
        at = datetime(2026, 9, 14, 10, 31, 0, tzinfo=UTC)
        assert not is_expired(expires, at - timedelta(milliseconds=1))
        assert is_expired(expires, at)
        assert is_expired(expires, at + timedelta(milliseconds=1))


def test_price_offset_shifts_every_level():
    parsed = {"entry_zone": {"low": 4286.0, "high": 4289.0}, "stop_loss": 4296.0, "take_profits": {"tp1": 4283.0, "tp2": 4280.0}, "action": "SELL"}
    shifted = apply_price_offset(parsed, -3.0)
    assert shifted["entry_zone"] == {"low": 4283.0, "high": 4286.0}
    assert shifted["stop_loss"] == 4293.0
    assert shifted["take_profits"] == {"tp1": 4280.0, "tp2": 4277.0}
    assert apply_price_offset(parsed, 0.0) is parsed
