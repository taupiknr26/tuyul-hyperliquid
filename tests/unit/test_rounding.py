"""Tick and size rules (FR-040, TC-037, TC-038, PRD §9.2)."""

import pytest
from hyperliquid.utils.signing import float_to_wire

from hlbot.domain.rounding import (
    floor_size,
    is_valid_price,
    is_valid_size,
    min_size_units,
    price_decimals,
    round_price,
    round_toward_entry,
    size_step,
    tick_size,
)


def test_effective_tick_near_4191_is_one_tenth_for_both_gold_markets():
    assert tick_size(4191.0, 4) == 0.1  # xyz:GOLD, szDecimals 4
    assert tick_size(4191.0, 3) == 0.1  # PAXG, szDecimals 3


@pytest.mark.parametrize(
    "price, sz_decimals, expected",
    [(4191.3, 4, 1), (4191.3, 3, 1), (99999.0, 0, 0), (123456.0, 0, 0), (12.345, 0, 3), (0.051234, 0, 6), (1.2345, 5, 1)],
)
def test_price_decimals(price, sz_decimals, expected):
    assert price_decimals(price, sz_decimals) == expected


def test_rounding_modes():
    assert round_price(4191.37, 4, "down") == 4191.3
    assert round_price(4191.31, 4, "up") == 4191.4
    assert round_price(4191.35, 4, "nearest") == 4191.4 or round_price(4191.35, 4, "nearest") == 4191.3
    with pytest.raises(ValueError):
        round_price(1.0, 4, "sideways")


def test_integer_prices_are_always_valid():
    assert is_valid_price(4284, 4)
    assert is_valid_price(123456, 0)
    assert not is_valid_price(4191.35, 4)
    assert not is_valid_price(0, 4)


def test_rounding_up_across_a_digit_boundary_stays_valid():
    assert round_price(9999.96, 4, "up") == 10000.0
    assert is_valid_price(round_price(9999.96, 4, "up"), 4)


@pytest.mark.parametrize("price", [4191.37, 4283.049, 0.123456789, 99999.99, 17.123456, 4296.0])
@pytest.mark.parametrize("mode", ["down", "up", "nearest"])
def test_rounded_prices_are_valid_and_survive_the_sdk_wire_format(price, mode):
    """TC-037: a price with too many figures is rounded, never sent invalid."""
    rounded = round_price(price, 4, mode)
    assert is_valid_price(rounded, 4)
    float_to_wire(rounded)  # raises if the SDK would have to round it


def test_floor_size_never_rounds_up():
    assert floor_size(5.0, 4) == 5.0
    assert floor_size(2.41289, 4) == 2.4128
    assert floor_size(0.00009, 4) == 0.0
    assert floor_size(0.1 + 0.2, 1) == 0.3  # float noise is not a reason to drop an increment
    assert size_step(4) == 0.0001


def test_size_validity():
    """TC-038."""
    assert is_valid_size(0.05, 4)
    assert not is_valid_size(0.00005, 4)
    assert not is_valid_size(0, 4)
    float_to_wire(floor_size(2.41289, 4))


def test_minimum_units_for_ten_dollars():
    # PRD §9.2: near 4,191 the 10 USDC minimum is about 0.0024 units.
    assert min_size_units(4191.0, 4) == 24
    assert min_size_units(4191.0, 4) * 0.0001 * 4191.0 >= 10.0
    assert (min_size_units(4191.0, 4) - 1) * 0.0001 * 4191.0 < 10.0


def test_protective_prices_round_toward_entry():
    # SELL: SL above entry rounds down, TP below entry rounds up; never widens risk.
    assert round_toward_entry(4296.07, 4, level_above_entry=True) == 4296.0
    assert round_toward_entry(4282.93, 4, level_above_entry=False) == 4283.0
