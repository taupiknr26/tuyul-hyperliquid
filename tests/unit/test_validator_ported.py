"""Ported from the baseline tests/test_signal_validator.py (see docs/TEST_PORT_MAPPING.md)."""
import unittest


from hlbot.domain.validator import VALIDATOR_VERSION, build_validated_record, validate_signal


def make_sell_signal_parsed(**overrides):
    parsed = {
        "symbol": "GOLD",
        "action": "SELL",
        "entry_zone": {"low": 4286.0, "high": 4289.0},
        "stop_loss": 4296.0,
        "take_profits": {"tp1": 4283.0, "tp2": 4280.0, "tp3": 4277.0},
        "hashtags": ["#GOLD", "#Vip_Signals"],
    }
    parsed.update(overrides)
    return parsed


def make_buy_signal_parsed(**overrides):
    parsed = {
        "symbol": "GOLD",
        "action": "BUY",
        "entry_zone": {"low": 4286.0, "high": 4289.0},
        "stop_loss": 4278.0,
        "take_profits": {"tp1": 4292.0, "tp2": 4295.0, "tp3": 4298.0},
        "hashtags": ["#GOLD"],
    }
    parsed.update(overrides)
    return parsed


class TestValidateSignal(unittest.TestCase):
    def test_valid_sell_signal(self):
        valid, errors = validate_signal(make_sell_signal_parsed(), [])
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_valid_buy_signal(self):
        valid, errors = validate_signal(make_buy_signal_parsed(), [])
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_existing_parse_errors_make_it_invalid(self):
        valid, errors = validate_signal(
            make_sell_signal_parsed(stop_loss=None), ["missing stop_loss (no 'SL :' found)"]
        )
        self.assertFalse(valid)
        self.assertTrue(any("incomplete parse data" in e for e in errors))

    def test_sell_with_stop_loss_on_wrong_side_is_invalid(self):
        # SL below the entry zone instead of above it.
        parsed = make_sell_signal_parsed(stop_loss=4280.0)
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("stop_loss must be above" in e for e in errors))

    def test_sell_with_take_profit_on_wrong_side_is_invalid(self):
        # A TP above the entry zone instead of below it.
        parsed = make_sell_signal_parsed(take_profits={"tp1": 4290.0})
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("take_profits must be below" in e for e in errors))

    def test_sell_with_take_profits_out_of_order_is_invalid(self):
        parsed = make_sell_signal_parsed(take_profits={"tp1": 4280.0, "tp2": 4283.0})
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("must decrease" in e for e in errors))

    def test_buy_with_stop_loss_on_wrong_side_is_invalid(self):
        parsed = make_buy_signal_parsed(stop_loss=4292.0)
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("stop_loss must be below" in e for e in errors))

    def test_invalid_entry_zone_bounds(self):
        parsed = make_sell_signal_parsed(entry_zone={"low": 4290.0, "high": 4286.0})
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("entry_zone.low must be less than" in e for e in errors))

    def test_missing_field_is_invalid(self):
        parsed = make_sell_signal_parsed(action=None)
        valid, errors = validate_signal(parsed, [])
        self.assertFalse(valid)
        self.assertTrue(any("missing action" in e for e in errors))


class TestBuildValidatedRecord(unittest.TestCase):
    def test_carries_forward_original_fields_and_adds_validation_fields(self):
        record = {
            "message_id": 1477,
            "type": "signal",
            "parsed": make_sell_signal_parsed(),
            "parse_errors": [],
            "original_text": "#GOLD SELL NOW...",
        }
        validated = build_validated_record(record, True, [], "2026-09-17T00:00:00+00:00")

        self.assertEqual(validated["message_id"], 1477)
        self.assertEqual(validated["original_text"], "#GOLD SELL NOW...")
        self.assertTrue(validated["valid"])
        self.assertEqual(validated["validation_errors"], [])
        self.assertEqual(validated["validator_version"], VALIDATOR_VERSION)
        self.assertEqual(validated["validated_at"], "2026-09-17T00:00:00+00:00")
        # Original record must not be mutated.
        self.assertNotIn("valid", record)


if __name__ == "__main__":
    unittest.main()
