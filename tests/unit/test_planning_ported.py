"""Ported from the baseline tests/test_execution_plan.py (see docs/TEST_PORT_MAPPING.md).
TestSplitVolume is retired here and replaced by tests/unit/test_planning.py."""
import unittest
from datetime import datetime, timezone


from hlbot.domain.planning import (
    adverse_slippage,
    allocate_tp_levels,
    build_signal_id,
    compute_effective_range,
    compute_expires_at,
    executable_price,
    is_expired,
    is_price_in_range,
    validate_levels,
)

ZONE = {"low": 4372.0, "high": 4375.0}


class TestEffectiveRange(unittest.TestCase):
    def test_buffer_widens_zone_on_both_sides(self):
        self.assertEqual(compute_effective_range(ZONE, 2.0), {"lower": 4370.0, "upper": 4377.0})

    def test_zero_buffer_is_the_original_zone(self):
        self.assertEqual(compute_effective_range(ZONE, 0.0), {"lower": 4372.0, "upper": 4375.0})

    def test_spec_examples_inside_zone_inside_buffer_and_outside(self):
        rng = compute_effective_range(ZONE, 2.0)
        self.assertTrue(is_price_in_range(4373, rng["lower"], rng["upper"]))  # inside zone
        self.assertTrue(is_price_in_range(4376, rng["lower"], rng["upper"]))  # inside buffer
        self.assertFalse(is_price_in_range(4379, rng["lower"], rng["upper"]))  # outside

    def test_range_edges_are_inclusive(self):
        self.assertTrue(is_price_in_range(4370.0, 4370.0, 4377.0))
        self.assertTrue(is_price_in_range(4377.0, 4370.0, 4377.0))
        self.assertFalse(is_price_in_range(4377.01, 4370.0, 4377.0))
        self.assertFalse(is_price_in_range(4369.99, 4370.0, 4377.0))


class TestExecutablePrice(unittest.TestCase):
    def test_buy_uses_ask_and_sell_uses_bid(self):
        self.assertEqual(executable_price("BUY", bid=4373.20, ask=4373.40), 4373.40)
        self.assertEqual(executable_price("SELL", bid=4373.20, ask=4373.40), 4373.20)


class TestSignalId(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            build_signal_id("gold", "SELL", ZONE, "2026-09-22T10:01:00+00:00"),
            "GOLD-SELL-4372-4375-20260922",
        )

    def test_fractional_zone(self):
        zone = {"low": 4372.5, "high": 4375.25}
        self.assertEqual(
            build_signal_id("GOLD", "BUY", zone, "2026-09-22T10:01:00+00:00"),
            "GOLD-BUY-4372.5-4375.25-20260922",
        )

    def test_same_signal_same_day_gets_same_id(self):
        first = build_signal_id("GOLD", "SELL", ZONE, "2026-09-22T10:01:00+00:00")
        repost = build_signal_id("GOLD", "SELL", ZONE, "2026-09-22T15:30:00+00:00")
        self.assertEqual(first, repost)

    def test_different_day_or_zone_gets_different_id(self):
        base = build_signal_id("GOLD", "SELL", ZONE, "2026-09-22T10:01:00+00:00")
        self.assertNotEqual(base, build_signal_id("GOLD", "SELL", ZONE, "2026-09-23T10:01:00+00:00"))
        self.assertNotEqual(
            base, build_signal_id("GOLD", "SELL", {"low": 4380.0, "high": 4383.0}, "2026-09-22T10:01:00+00:00")
        )

    def test_missing_date(self):
        self.assertTrue(build_signal_id("GOLD", "SELL", ZONE, None).endswith("-nodate"))


class TestExpiry(unittest.TestCase):
    def test_expires_after_configured_minutes(self):
        self.assertEqual(
            compute_expires_at("2026-09-22T10:00:00+00:00", 30), "2026-09-22T10:30:00+00:00"
        )

    def test_no_reference_means_no_expiry(self):
        self.assertIsNone(compute_expires_at(None, 30))
        self.assertFalse(is_expired(None, datetime.now(timezone.utc)))

    def test_is_expired_boundaries(self):
        expires = "2026-09-22T10:30:00+00:00"
        before = datetime(2026, 9, 22, 10, 29, 59, tzinfo=timezone.utc)
        at = datetime(2026, 9, 22, 10, 30, 0, tzinfo=timezone.utc)
        self.assertFalse(is_expired(expires, before))
        self.assertTrue(is_expired(expires, at))


class TestAllocateTpLevels(unittest.TestCase):
    TPS = {"tp1": 4365.0, "tp2": 4360.0, "tp3": 4355.0, "tp4": 4350.0, "tp5": 4345.0}

    def test_five_levels_twenty_percent_each(self):
        levels = allocate_tp_levels(self.TPS, [20, 20, 20, 20, 20])
        self.assertEqual([l["level"] for l in levels], [1, 2, 3, 4, 5])
        self.assertEqual([l["price"] for l in levels], [4365.0, 4360.0, 4355.0, 4350.0, 4345.0])
        self.assertEqual([l["close_percent"] for l in levels], [20.0] * 5)

    def test_levels_sorted_numerically_not_alphabetically(self):
        tps = {f"tp{i}": float(i) for i in (10, 2, 1)}
        self.assertEqual([l["level"] for l in allocate_tp_levels(tps, [40, 30, 30])], [1, 2, 10])

    def test_fewer_signal_tps_rescales_percentages_to_100(self):
        levels = allocate_tp_levels({"tp1": 1.0, "tp2": 2.0}, [20, 20, 20, 20, 20])
        self.assertEqual([l["close_percent"] for l in levels], [50.0, 50.0])

    def test_extra_signal_tps_beyond_configured_are_not_traded(self):
        levels = allocate_tp_levels(self.TPS, [50, 50])
        self.assertEqual([l["level"] for l in levels], [1, 2])
        self.assertEqual([l["close_percent"] for l in levels], [50.0, 50.0])

    def test_single_level_config_closes_everything_at_tp1(self):
        levels = allocate_tp_levels(self.TPS, [100])
        self.assertEqual(
            levels, [{"level": 1, "price": 4365.0, "raw_price": 4365.0, "close_percent": 100.0}]
        )

    def test_empty_take_profits_raises(self):
        with self.assertRaises(ValueError):
            allocate_tp_levels({}, [20, 20, 20, 20, 20])

    def test_no_buffer_by_default_price_equals_raw_price(self):
        levels = allocate_tp_levels(self.TPS, [20, 20, 20, 20, 20])
        self.assertEqual([l["price"] for l in levels], [l["raw_price"] for l in levels])

    def test_sell_buffer_shifts_price_up_toward_entry(self):
        # A SELL's TP closes on the ask, which sits above the bid the
        # signal was written against -- shifting the order's TP up
        # (closer to entry) gives the ask room to reach it.
        levels = allocate_tp_levels(self.TPS, [20, 20, 20, 20, 20], direction="SELL", tp_buffer_points=1.0)
        self.assertEqual([l["price"] for l in levels], [4366.0, 4361.0, 4356.0, 4351.0, 4346.0])
        self.assertEqual([l["raw_price"] for l in levels], [4365.0, 4360.0, 4355.0, 4350.0, 4345.0])

    def test_buy_buffer_shifts_price_down_toward_entry(self):
        tps = {"tp1": 4385.0, "tp2": 4390.0}
        levels = allocate_tp_levels(tps, [50, 50], direction="BUY", tp_buffer_points=1.5)
        self.assertEqual([l["price"] for l in levels], [4383.5, 4388.5])
        self.assertEqual([l["raw_price"] for l in levels], [4385.0, 4390.0])

    def test_zero_buffer_leaves_price_unchanged(self):
        levels = allocate_tp_levels(self.TPS, [100], direction="SELL", tp_buffer_points=0.0)
        self.assertEqual(levels[0]["price"], levels[0]["raw_price"])



class TestSlippage(unittest.TestCase):
    def test_buy_fill_above_reference_is_adverse(self):
        self.assertEqual(adverse_slippage("BUY", 4373.40, 4374.40), 1.0)

    def test_sell_fill_below_reference_is_adverse(self):
        self.assertEqual(adverse_slippage("SELL", 4373.20, 4372.20), 1.0)

    def test_price_improvement_is_negative(self):
        self.assertLess(adverse_slippage("BUY", 4373.40, 4373.00), 0)
        self.assertLess(adverse_slippage("SELL", 4373.20, 4373.60), 0)


class TestValidateLevels(unittest.TestCase):
    def test_valid_sell(self):
        self.assertEqual(
            validate_levels("SELL", 4373.0, 4382.0, [4365.0, 4360.0, 4355.0, 4350.0, 4345.0]), []
        )

    def test_valid_buy(self):
        self.assertEqual(validate_levels("BUY", 4373.0, 4365.0, [4380.0, 4385.0]), [])

    def test_sell_sl_below_entry_is_flagged(self):
        problems = validate_levels("SELL", 4373.0, 4370.0, [4365.0])
        self.assertTrue(any("SL" in p for p in problems))

    def test_sell_tp_already_passed_is_flagged(self):
        problems = validate_levels("SELL", 4373.0, 4382.0, [4365.0, 4374.0])
        self.assertEqual(len(problems), 1)
        self.assertIn("4374", problems[0])

    def test_buy_tp_below_entry_is_flagged(self):
        self.assertTrue(validate_levels("BUY", 4373.0, 4365.0, [4372.0]))

    def test_min_distance_is_enforced(self):
        self.assertEqual(validate_levels("SELL", 4373.0, 4373.5, [4365.0], min_distance=0.0), [])
        self.assertTrue(validate_levels("SELL", 4373.0, 4373.5, [4365.0], min_distance=1.0))


if __name__ == "__main__":
    unittest.main()
