"""Ported from the baseline tests/test_signal_parser.py (see docs/TEST_PORT_MAPPING.md)."""
import unittest


from hlbot.domain.parser import (
    configure_channel_parsers,
    PARSER_VERSION,
    classify,
    classify_for_channel,
    extract_signal_fields,
    extract_signal_fields_for_channel,
    extract_tp_update_fields,
    parse_message,
)


SIGNAL_TEXT = (
    "#GOLD SELL NOW\n"
    "Gold sell Zone 4286- 4289\n\n"
    "SL : 4296\n\n"
    "TP1 : 4283\n"
    "TP2 : 4280\n"
    "TP3 : 4277\n"
    "TP4 : 4274\n"
    "TP5 : 4270\n\n"
    "or set your stop loss according to your margin\n"
    "#Vip_Signals"
)

SIGNAL_TEXT_NO_SPACE_ZONE = (
    "#GOLD SELL NOW\n"
    "Gold sell Zone 4325-4330\n\n"
    "SL : 4340\n\n"
    "TP1 : 4320\n"
    "#Vip_Signals"
)

SIGNAL_TEXT_MISSING_SL = (
    "#GOLD SELL NOW\n"
    "Gold sell Zone 4286- 4289\n\n"
    "TP1 : 4283\n"
    "TP2 : 4280\n"
    "#Vip_Signals"
)

ENTRY_LIMIT_CHANNEL = "-1002095437517"


def setUpModule():
    # LIM-21: the channel -> format mapping is configuration now, not source code.
    configure_channel_parsers({ENTRY_LIMIT_CHANNEL: "entry_limit"})


def tearDownModule():
    configure_channel_parsers({})
ENTRY_LIMIT_SIGNAL_TEXT = (
    "#XAUUSD sell ENTRY 4293 Limit 4296\n\n\n"
    "✔️TP::::4290\n"
    "✔️TP::::4287\n"
    "✔️TP::::4284\n"
    "✔️TP::::4281\n"
    "✔️TP::::4278\n"
    "✔️TP::::4275\n"
    "✔️TP::::OPEN\n\n"
    "\U0001f6abSL:::4305"
)

TP_HIT_TEXT = "#XAUUSD ❤️\nTP 1 HIT 30+ PIPS PROFIT DONE \U0001f942"
TP_ALL_HIT_TEXT = "#XAUUSD \U0001f525\nTP ALL HIT 150+ PIPS PROFIT DONE \U0001f4af"
STATUS_READY_TEXT = "Ready ✅\U0001f4ab"
PROMOTIONAL_TEXT = (
    "Safe account management available here \n"
    "No Risk \n"
    "No loss \n"
    "All investor don't worry I'll help you \n"
    "I'll Recover your all lost money\n"
    "\U0001f600Safe Trading Mt4 Mt5\n\n"
    "Contact me @FXJAT"
)
LINK_TEXT = "https://x.com/FX_MINDSET1/status/2100169008758894980"
OTHER_TEXT = "Good morning"


class TestClassify(unittest.TestCase):
    def test_classifies_signal(self):
        self.assertEqual(classify(SIGNAL_TEXT, False), "signal")

    def test_classifies_tp_all_hit(self):
        self.assertEqual(classify(TP_ALL_HIT_TEXT, False), "tp_all_hit")

    def test_classifies_tp_hit(self):
        self.assertEqual(classify(TP_HIT_TEXT, False), "tp_hit")

    def test_classifies_status_ready(self):
        self.assertEqual(classify(STATUS_READY_TEXT, False), "status_ready")

    def test_classifies_promotional_account_management(self):
        self.assertEqual(classify(PROMOTIONAL_TEXT, False), "promotional")

    def test_classifies_promotional_contact_admin(self):
        text = "Big profits waiting, just contact admin now"
        self.assertEqual(classify(text, False), "promotional")

    def test_classifies_promotional_double_your(self):
        text = "We will double your investment in a week"
        self.assertEqual(classify(text, False), "promotional")

    def test_classifies_link(self):
        self.assertEqual(classify(LINK_TEXT, False), "link")

    def test_classifies_media_only(self):
        self.assertEqual(classify("", True), "media_only")

    def test_classifies_other(self):
        self.assertEqual(classify(OTHER_TEXT, False), "other")


class TestExtractSignalFields(unittest.TestCase):
    def test_full_signal_extraction(self):
        parsed, errors = extract_signal_fields(SIGNAL_TEXT)

        self.assertEqual(errors, [])
        self.assertEqual(parsed["symbol"], "GOLD")
        self.assertEqual(parsed["action"], "SELL")
        self.assertEqual(parsed["entry_zone"], {"low": 4286.0, "high": 4289.0})
        self.assertEqual(parsed["stop_loss"], 4296.0)
        self.assertEqual(
            parsed["take_profits"],
            {"tp1": 4283.0, "tp2": 4280.0, "tp3": 4277.0, "tp4": 4274.0, "tp5": 4270.0},
        )
        self.assertEqual(parsed["hashtags"], ["#GOLD", "#Vip_Signals"])

    def test_entry_zone_handles_no_space_spacing(self):
        parsed, errors = extract_signal_fields(SIGNAL_TEXT_NO_SPACE_ZONE)
        self.assertEqual(errors, [])
        self.assertEqual(parsed["entry_zone"], {"low": 4325.0, "high": 4330.0})

    def test_entry_zone_handles_dash_space_spacing(self):
        parsed, errors = extract_signal_fields(SIGNAL_TEXT)
        self.assertEqual(parsed["entry_zone"], {"low": 4286.0, "high": 4289.0})

    def test_missing_stop_loss_reports_parse_error_without_guessing(self):
        parsed, errors = extract_signal_fields(SIGNAL_TEXT_MISSING_SL)

        self.assertIsNone(parsed["stop_loss"])
        self.assertTrue(
            any("stop_loss" in err for err in errors),
            f"expected a stop_loss parse error, got {errors}",
        )
        # Other fields should still be extracted correctly.
        self.assertEqual(parsed["symbol"], "GOLD")
        self.assertEqual(parsed["action"], "SELL")
        self.assertEqual(parsed["entry_zone"], {"low": 4286.0, "high": 4289.0})


class TestEntryLimitChannelFormat(unittest.TestCase):
    """The channel identified only by numeric id (-1002095437517, no
    @username) posts signals as "ENTRY <low> Limit <high>" / "TP::::<n>"
    bullets / "SL:::<n>" instead of the default "Zone"/"SL :"/"TPn :"
    format -- classify_for_channel()/extract_signal_fields_for_channel()
    dispatch to its own rules instead of the default ones."""

    def test_default_classify_does_not_recognise_this_format_as_signal(self):
        # Confirms the bug: without channel-aware dispatch this format
        # falls through every default rule (no "Zone", no numbered "TPn :").
        self.assertNotEqual(classify(ENTRY_LIMIT_SIGNAL_TEXT, True), "signal")

    def test_classify_for_channel_recognises_it_as_signal(self):
        self.assertEqual(
            classify_for_channel(ENTRY_LIMIT_CHANNEL, ENTRY_LIMIT_SIGNAL_TEXT, True),
            "signal",
        )

    def test_classify_for_channel_falls_back_to_default_for_other_channels(self):
        self.assertEqual(
            classify_for_channel("@JugnoTrad_Hub84", SIGNAL_TEXT, False),
            "signal",
        )

    def test_extract_signal_fields_for_channel(self):
        parsed, errors = extract_signal_fields_for_channel(
            ENTRY_LIMIT_CHANNEL, ENTRY_LIMIT_SIGNAL_TEXT
        )

        self.assertEqual(errors, [])
        self.assertEqual(parsed["symbol"], "XAUUSD")
        self.assertEqual(parsed["action"], "SELL")
        self.assertEqual(parsed["entry_zone"], {"low": 4293.0, "high": 4296.0})
        self.assertEqual(parsed["stop_loss"], 4305.0)
        self.assertEqual(
            parsed["take_profits"],
            {
                "tp1": 4290.0,
                "tp2": 4287.0,
                "tp3": 4284.0,
                "tp4": 4281.0,
                "tp5": 4278.0,
                "tp6": 4275.0,
            },
        )
        # "TP::::OPEN" is not a price and must not be guessed at.
        self.assertNotIn("tp7", parsed["take_profits"])
        self.assertEqual(parsed["hashtags"], ["#XAUUSD"])

    def test_extract_signal_fields_for_channel_falls_back_to_default(self):
        parsed, errors = extract_signal_fields_for_channel("@JugnoTrad_Hub84", SIGNAL_TEXT)
        self.assertEqual(errors, [])
        self.assertEqual(parsed["entry_zone"], {"low": 4286.0, "high": 4289.0})


class TestExtractTpUpdateFields(unittest.TestCase):
    def test_tp_hit_extraction(self):
        parsed, errors = extract_tp_update_fields(TP_HIT_TEXT, "tp_hit")

        self.assertEqual(errors, [])
        self.assertEqual(parsed["symbol"], "XAUUSD")
        self.assertEqual(parsed["tp_number"], 1)
        self.assertEqual(parsed["pips"], 30)

    def test_tp_all_hit_extraction(self):
        parsed, errors = extract_tp_update_fields(TP_ALL_HIT_TEXT, "tp_all_hit")

        self.assertEqual(errors, [])
        self.assertEqual(parsed["symbol"], "XAUUSD")
        self.assertIsNone(parsed["tp_number"])
        self.assertEqual(parsed["pips"], 150)


class TestParseMessage(unittest.TestCase):
    def _make_message(self, message_id=1, text=SIGNAL_TEXT, has_media=False, reply_to=1476):
        return {
            "message_id": message_id,
            "channel_id": -1004307627275,
            "channel_username": "@JugnoTrad_Hub84",
            "date": "2026-09-14T10:01:00+00:00",
            "sender_id": -1004307627275,
            "text": text,
            "has_media": has_media,
            "views": 91,
            "forwards": 0,
            "reply_to_message_id": reply_to,
        }

    def test_parse_message_includes_all_required_fields(self):
        message = self._make_message()
        record = parse_message(message, parsed_at="2026-09-17T00:00:00+00:00")

        self.assertEqual(record["message_id"], 1)
        self.assertEqual(record["channel_id"], -1004307627275)
        self.assertEqual(record["channel_username"], "@JugnoTrad_Hub84")
        self.assertEqual(record["date"], "2026-09-14T10:01:00+00:00")
        self.assertEqual(record["reply_to_message_id"], 1476)
        self.assertEqual(record["type"], "signal")
        self.assertIsNotNone(record["parsed"])
        self.assertEqual(record["parse_errors"], [])
        self.assertEqual(record["original_text"], SIGNAL_TEXT)
        self.assertEqual(record["parser_version"], PARSER_VERSION)
        self.assertEqual(record["parsed_at"], "2026-09-17T00:00:00+00:00")

    def test_parse_message_dispatches_to_channel_specific_format(self):
        message = self._make_message(
            message_id=32937, text=ENTRY_LIMIT_SIGNAL_TEXT, has_media=True, reply_to=32936
        )
        message["channel_id"] = -1002095437517
        message["channel_username"] = ENTRY_LIMIT_CHANNEL

        record = parse_message(message)

        self.assertEqual(record["type"], "signal")
        self.assertEqual(record["parse_errors"], [])
        self.assertEqual(record["parsed"]["symbol"], "XAUUSD")
        self.assertEqual(record["parsed"]["action"], "SELL")
        self.assertEqual(record["parsed"]["entry_zone"], {"low": 4293.0, "high": 4296.0})
        self.assertEqual(record["parsed"]["stop_loss"], 4305.0)
        self.assertEqual(len(record["parsed"]["take_profits"]), 6)

    def test_non_signal_message_has_null_parsed_and_no_errors(self):
        message = self._make_message(text=OTHER_TEXT, reply_to=None)
        record = parse_message(message)

        self.assertEqual(record["type"], "other")
        self.assertIsNone(record["parsed"])
        self.assertEqual(record["parse_errors"], [])

    def test_promotional_message_is_flagged_never_silently_dropped(self):
        message = self._make_message(text=PROMOTIONAL_TEXT, reply_to=None)
        record = parse_message(message)

        self.assertEqual(record["type"], "promotional")
        self.assertEqual(record["original_text"], PROMOTIONAL_TEXT)

    def test_media_only_message_is_preserved(self):
        message = self._make_message(text="", has_media=True, reply_to=1466)
        record = parse_message(message)

        self.assertEqual(record["type"], "media_only")
        self.assertEqual(record["original_text"], "")


if __name__ == "__main__":
    unittest.main()
