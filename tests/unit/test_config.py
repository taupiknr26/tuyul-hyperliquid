"""Configuration and secrets (FR-082, FR-095, SR-03, SR-08; TC-121, TC-122).
Successor of the baseline's test_execution_config / test_payload_config /
test_auto_config / TestLoadNotifierConfig (see docs/TEST_PORT_MAPPING.md)."""

import os

import pytest

from hlbot.config import (
    ConfigError,
    SecretFileError,
    Settings,
    load_agent_key,
    load_settings,
    parse_channels,
    parse_env_file,
    short_address,
)

BASE = {"TELEGRAM_ENABLED": "false"}
ADDRESS = "0x" + "ab" * 20


def load(env, **kwargs):
    """load_settings against exactly ``env``: no real .env, no process environment."""
    return load_settings({**BASE, **env}, env_file=None, **kwargs)


# ---------------------------------------------------------------- defaults (PRD §10.6)
def test_nothing_set_gives_the_documented_defaults():
    s = load({})
    assert s.env == "paper" and s.label == "PAPER"
    assert (s.buffer_enabled, s.buffer_value) == (True, 2.0)
    assert s.tp_close_percents == (20.0,) * 5 and s.tp_buffer_value == 0.0
    assert s.signal_expiry_minutes == 30.0 and s.max_slippage_points == 3.0
    assert s.entry_attempts == 3 and s.entry_ttl_ms == 5000
    assert (s.protect_soft_ms, s.protect_hard_ms, s.flatten_deadline_ms) == (3000, 8000, 15000)
    assert (s.flatten_slippage_pct, s.flatten_max_slippage_pct) == (1.0, 5.0)
    assert s.partial_fill_policy == "accept" and s.tp_style == "limit" and not s.sl_breakeven_after_tp1
    assert s.symbol_map == {"GOLD": "xyz:GOLD"} and s.coins == ("xyz:GOLD",)
    assert s.reconcile_interval_s == 15.0 and s.ws_stale_ms == 10000
    assert s.api_host == "127.0.0.1"  # SR-09


def test_every_risk_control_is_on_by_default():
    """Fixes LIM-07: nothing is opt-in any more."""
    r = load({}).risk
    assert r.risk_per_trade_pct == 0.5 and r.sizing_mode == "risk"
    assert r.max_daily_loss_pct == 2.0 and r.max_open_trades == 3 and r.max_trades_per_asset == 1
    assert r.max_exposure_x_equity == 3.0 and r.leverage == 5 and r.min_rr == 0.5
    assert r.sl_slippage_allowance_points == 1.0 and r.max_sl_distance == 30.0
    assert r.margin_usage_cap == 0.5 and r.liq_buffer_x == 1.5 and r.max_trade_notional == 5000.0
    assert (r.max_quote_age_ms, r.max_account_age_ms) == (2000, 5000)
    assert r.daily_target_pct is None and r.max_mark_oracle_dev_points is None  # the two optional controls


def test_overrides_and_blank_values():
    s = load(
        {
            "EXEC_BUFFER_ENABLED": "false", "EXEC_BUFFER_VALUE": "1.5", "EXEC_SIGNAL_EXPIRY_MINUTES": "45",
            "EXEC_TP_CLOSE_PERCENTS": "50, 30, 20", "EXEC_TP_BUFFER_VALUE": "0.3", "EXEC_MAX_SLIPPAGE_POINTS": "",
            "RISK_PER_TRADE_PCT": "1.25", "RISK_DAILY_TARGET_PCT": "3", "RISK_LEVERAGE": "10", "AUTO_POLL_INTERVAL": "20",
            "HL_SYMBOL_MAP": '{"gold": "PAXG", "XAUUSD": "PAXG"}', "HL_TP_STYLE": "TRIGGER",
            "TELEGRAM_CHANNEL_PARSERS": '{"-1002095437517": "entry_limit"}',
        }
    )
    assert not s.buffer_enabled and s.buffer_value == 1.5 and s.signal_expiry_minutes == 45
    assert s.tp_close_percents == (50.0, 30.0, 20.0) and s.tp_buffer_value == 0.3
    assert s.max_slippage_points == 3.0  # blank falls back to the default
    assert s.risk.risk_per_trade_pct == 1.25 and s.risk.daily_target_pct == 3.0 and s.risk.leverage == 10
    assert s.reconcile_interval_s == 20.0
    assert s.symbol_map == {"GOLD": "PAXG", "XAUUSD": "PAXG"} and s.coins == ("PAXG",) and s.map_symbol("gold") == "PAXG"
    assert s.map_symbol("SILVER") is None and s.tp_style == "trigger"
    assert s.telegram_channel_parsers == {"-1002095437517": "entry_limit"}


@pytest.mark.parametrize(
    "env, variable",
    [
        ({"EXEC_TP_CLOSE_PERCENTS": "50,30"}, "EXEC_TP_CLOSE_PERCENTS"),
        ({"EXEC_TP_CLOSE_PERCENTS": "50,-10,60"}, "EXEC_TP_CLOSE_PERCENTS"),
        ({"EXEC_TP_CLOSE_PERCENTS": "a,b"}, "EXEC_TP_CLOSE_PERCENTS"),
        ({"EXEC_BUFFER_VALUE": "wide"}, "EXEC_BUFFER_VALUE"),
        ({"EXEC_BUFFER_ENABLED": "maybe"}, "EXEC_BUFFER_ENABLED"),
        ({"RISK_PER_TRADE_PCT": "5.1"}, "RISK_PER_TRADE_PCT"),  # hard bound <= 5
        ({"RISK_PER_TRADE_PCT": "0"}, "RISK_PER_TRADE_PCT"),
        ({"RISK_MAX_DAILY_LOSS_PCT": "21"}, "RISK_MAX_DAILY_LOSS_PCT"),  # hard bound <= 20
        ({"RISK_MARGIN_USAGE_CAP": "0.95"}, "RISK_MARGIN_USAGE_CAP"),  # hard bound <= 0.9
        ({"RISK_LIQ_BUFFER_X": "1.0"}, "RISK_LIQ_BUFFER_X"),  # hard bound >= 1.1
        ({"RISK_MAX_TRADES_PER_ASSET": "2"}, "RISK_MAX_TRADES_PER_ASSET"),  # fixed at 1 in v1
        ({"EXEC_ENTRY_ATTEMPTS": "11"}, "EXEC_ENTRY_ATTEMPTS"),  # hard bound <= 10
        ({"RISK_LEVERAGE": "2.5"}, "RISK_LEVERAGE"),
        ({"RISK_SIZING_MODE": "martingale"}, "RISK_SIZING_MODE"),
        ({"RISK_SIZING_MODE": "fixed"}, "RISK_FIXED_SIZE"),
        ({"RISK_COMPOUND_ENABLED": "false"}, "RISK_FIXED_EQUITY_BASELINE"),
        ({"HL_SYMBOL_MAP": "{not json"}, "HL_SYMBOL_MAP"),
        ({"HL_SYMBOL_MAP": '{"GOLD": 5}'}, "HL_SYMBOL_MAP"),
        ({"HL_ENV": "live"}, "HL_ENV"),
        ({"HL_ACCOUNT_ADDRESS": "0x1234"}, "HL_ACCOUNT_ADDRESS"),
        ({"EXEC_PROTECT_SOFT_MS": "9000"}, "EXEC_PROTECT_HARD_MS"),
        ({"EXEC_FLATTEN_SLIPPAGE_PCT": "6"}, "EXEC_FLATTEN_MAX_SLIPPAGE_PCT"),
        ({"TELEGRAM_CHANNEL_PARSERS": '{"x": "nope"}'}, "TELEGRAM_CHANNEL_PARSERS"),
        ({"TELEGRAM_NOTIFY_ENABLED": "maybe"}, "TELEGRAM_NOTIFY_ENABLED"),
        ({"TELEGRAM_NOTIFY_ENABLED": "true", "TELEGRAM_NOTIFY_CHAT_ID": "123"}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_NOTIFY_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35}, "TELEGRAM_NOTIFY_CHAT_ID"),
        ({"AUTO_POLL_INTERVAL": "soon"}, "AUTO_POLL_INTERVAL"),
        ({"API_PORT": "99999"}, "API_PORT"),
    ],
)
def test_a_malformed_value_stops_startup_and_names_the_variable(env, variable):
    with pytest.raises(ConfigError) as info:
        load(env)
    assert variable in str(info.value)


def test_all_errors_are_reported_together():
    with pytest.raises(ConfigError) as info:
        load({"EXEC_BUFFER_VALUE": "x", "RISK_LEVERAGE": "y", "API_PORT": "z"})
    assert all(name in str(info.value) for name in ("EXEC_BUFFER_VALUE", "RISK_LEVERAGE", "API_PORT"))


def test_sizing_with_compound_on_does_not_need_a_baseline_and_fixed_modes_validate():
    assert load({"RISK_COMPOUND_ENABLED": "true"}).risk.fixed_equity_baseline is None
    s = load({"RISK_COMPOUND_ENABLED": "false", "RISK_FIXED_EQUITY_BASELINE": "2500", "RISK_SIZING_MODE": "fixed", "RISK_FIXED_SIZE": "0.05"})
    assert s.risk.fixed_equity_baseline == 2500 and s.risk.fixed_size == 0.05 and s.risk.sizing_mode == "fixed"


def test_retired_mt5_settings_are_ignored_with_a_warning():
    s = load({"MT5_LOGIN": "123", "MT5_VOLUME": "0.05", "EXEC_ORDER_TYPE": "MARKET", "EXEC_SLIPPAGE_ACTION": "close"})
    assert len(s.warnings) == 4 and all("retired" in w for w in s.warnings)


# ---------------------------------------------------------------- telegram
def test_telegram_requires_id_hash_and_channel_when_enabled():
    with pytest.raises(ConfigError) as info:
        load_settings({}, env_file=None)
    message = str(info.value)
    assert "TELEGRAM_API_ID" in message and "TELEGRAM_API_HASH" in message and "TELEGRAM_CHANNEL" in message
    s = load_settings({"TELEGRAM_API_ID": "123456", "TELEGRAM_API_HASH": "a" * 32, "TELEGRAM_CHANNEL": "@one, -1001234567890 ,https://t.me/+abc,"}, env_file=None)
    assert s.telegram_channels == ("@one", -1001234567890, "https://t.me/+abc")


def test_channel_parsing_ported():
    assert parse_channels("@a,-100123, 456 ,t.me/x,") == ["@a", -100123, 456, "t.me/x"]
    assert parse_channels("") == []


def test_session_file_defaults_to_outside_the_repository():
    """SR-04."""
    path = load({}).session_path
    assert not str(path).startswith(os.getcwd()) and ".config/hlbot" in str(path)


# ---------------------------------------------------------------- modes and the mainnet guard (FR-080..FR-082)
def test_tc122_mainnet_without_the_flag_runs_paper_and_says_so():
    s = load({"HL_ENV": "mainnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_MAINNET_CONFIRM": ADDRESS, "HL_AGENT_KEY_FILE": "/nonexistent"})
    assert s.env == "paper" and s.requested_env == "mainnet" and "PAPER" in s.mode_note and "--mainnet" in s.mode_note


def test_tc122_mainnet_with_the_flag_but_without_confirmation_runs_paper_and_says_so():
    for confirm in ({}, {"HL_MAINNET_CONFIRM": "yes"}, {"HL_MAINNET_CONFIRM": "0x" + "cd" * 20}):
        s = load({"HL_ENV": "mainnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": "/nonexistent", **confirm}, mainnet_flag=True)
        assert s.env == "paper" and "HL_MAINNET_CONFIRM" in s.mode_note


def test_mainnet_needs_both_the_flag_and_the_matching_confirmation():
    s = load({"HL_ENV": "mainnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_MAINNET_CONFIRM": ADDRESS.upper().replace("0X", "0x"), "HL_AGENT_KEY_FILE": "/k"}, mainnet_flag=True)
    assert s.env == "mainnet" and s.mode_note is None and s.rest_url == "https://api.hyperliquid.xyz"


def test_duplicate_protection_cannot_be_disabled_on_mainnet():
    env = {"HL_ENV": "mainnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_MAINNET_CONFIRM": ADDRESS, "HL_AGENT_KEY_FILE": "/k", "EXEC_DUPLICATE_PROTECTION": "false"}
    with pytest.raises(ConfigError, match="EXEC_DUPLICATE_PROTECTION"):
        load(env, mainnet_flag=True)
    assert not load({"EXEC_DUPLICATE_PROTECTION": "false"}).duplicate_protection  # allowed elsewhere


def test_testnet_is_a_single_setting_and_needs_account_and_key_file():
    with pytest.raises(ConfigError) as info:
        load({"HL_ENV": "testnet"})
    assert "HL_ACCOUNT_ADDRESS" in str(info.value) and "HL_AGENT_KEY_FILE" in str(info.value)
    s = load({"HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": "/k"})
    assert s.env == "testnet" and s.label == "TESTNET"
    assert s.rest_url == "https://api.hyperliquid-testnet.xyz" and s.ws_url == "wss://api.hyperliquid-testnet.xyz/ws"


def test_paper_uses_mainnet_market_data_and_a_database_per_environment(tmp_path):
    paper = load({"DATA_DIR": str(tmp_path)})
    testnet = load({"DATA_DIR": str(tmp_path), "HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": "/k"})
    assert paper.rest_url == "https://api.hyperliquid.xyz" and paper.market_data_env == "mainnet"
    assert paper.db_path != testnet.db_path and paper.lock_path != testnet.lock_path


# ---------------------------------------------------------------- secrets (SR-03, SR-05, SR-08)
def test_tc121_world_readable_secrets_file_is_refused(tmp_path):
    secrets = tmp_path / "hlbot.secrets"
    secrets.write_text("TELEGRAM_BOT_TOKEN=123456789:" + "A" * 35 + "\n")
    secrets.chmod(0o644)
    with pytest.raises(SecretFileError, match="chmod 600"):
        load({"SECRETS_FILE": str(secrets)})
    secrets.chmod(0o640)
    with pytest.raises(SecretFileError):
        load({"SECRETS_FILE": str(secrets)})
    secrets.chmod(0o600)
    s = load({"SECRETS_FILE": str(secrets), "TELEGRAM_NOTIFY_ENABLED": "true", "TELEGRAM_NOTIFY_CHAT_ID": "42"})
    assert s.telegram_bot_token.startswith("123456789:")


def test_secrets_in_dot_env_are_refused(tmp_path):
    """Fixes SEC-01: the plaintext .env may not hold a secret."""
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_ENABLED=false\nTELEGRAM_BOT_TOKEN=123456789:" + "A" * 35 + "\n")
    with pytest.raises(ConfigError, match="must not be stored"):
        load_settings({}, env_file=env_file)


def test_agent_key_is_never_read_from_the_environment_or_the_secrets_file(tmp_path):
    with pytest.raises(ConfigError, match="HL_AGENT_KEY_FILE"):
        load({"HL_AGENT_PRIVATE_KEY": "0x" + "11" * 32})


def test_paper_mode_never_loads_a_key_even_when_a_key_file_is_configured(tmp_path):
    """FR-080 / SR-08."""
    key_file = tmp_path / "agent.key"
    key_file.write_text("0x" + "11" * 32)
    key_file.chmod(0o600)
    s = load({"HL_AGENT_KEY_FILE": str(key_file)})
    with pytest.raises(ConfigError, match="never loads a signing key"):
        load_agent_key(s)


def test_agent_key_file_must_be_owner_only_and_well_formed(tmp_path):
    key_file = tmp_path / "agent.key"
    key_file.write_text("0x" + "11" * 32 + "\n")
    s = load({"HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": str(key_file)})
    key_file.chmod(0o644)
    with pytest.raises(SecretFileError):
        load_agent_key(s)
    key_file.chmod(0o600)
    assert load_agent_key(s) == "0x" + "11" * 32
    key_file.write_text("not a key")
    with pytest.raises(ConfigError, match="32-byte hex"):
        load_agent_key(s)
    missing = load({"HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": str(tmp_path / "nope")})
    with pytest.raises(SecretFileError, match="not found"):
        load_agent_key(missing)


def test_public_view_hides_every_secret_and_shortens_addresses(tmp_path):
    """`botctl config` and the stored config version (FR-093, SR-05)."""
    secrets = tmp_path / "s"
    token = "123456789:" + "B" * 35
    secrets.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_API_HASH={'c' * 32}\n")
    secrets.chmod(0o600)
    s = load({"SECRETS_FILE": str(secrets), "TELEGRAM_NOTIFY_ENABLED": "true", "TELEGRAM_NOTIFY_CHAT_ID": "-100987654321",
              "HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": ADDRESS, "HL_AGENT_KEY_FILE": "/k"})
    text = str(s.public_dict())
    assert token not in text and "c" * 32 not in text and "-100987654321" not in text and ADDRESS not in text
    assert s.public_dict()["account_address"] == short_address(ADDRESS) == "0xababab…"
    assert s.config_hash() == s.config_hash() and len(s.config_hash()) == 16
    assert sorted(s.secret_values()) == sorted([token, "c" * 32])


def test_env_file_reader_handles_quotes_comments_and_export(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\nexport A=1\nB="two words"\nC=\'{"GOLD": "xyz:GOLD"}\'\n\nD=x=y\nnoequals\n')
    assert parse_env_file(path) == {"A": "1", "B": "two words", "C": '{"GOLD": "xyz:GOLD"}', "D": "x=y"}
    assert parse_env_file(tmp_path / "missing") == {}


def test_process_environment_overrides_the_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TELEGRAM_ENABLED=false\nEXEC_BUFFER_VALUE=1.0\n")
    assert load_settings({}, env_file=path).buffer_value == 1.0
    assert load_settings({"EXEC_BUFFER_VALUE": "4.0"}, env_file=path).buffer_value == 4.0


def test_settings_object_is_immutable():
    with pytest.raises(Exception):
        Settings().env = "mainnet"  # type: ignore[misc]


def test_public_origins_are_normalised_and_anything_but_a_bare_origin_is_refused():
    """API_PUBLIC_ORIGINS: where a reverse proxy or tunnel publishes the dashboard."""
    assert load({}).api_public_origins == ()
    settings = load({"API_PUBLIC_ORIGINS": " https://Bot.Example.com/ , https://bot.example.com,https://alt.example.com:8443 "})
    assert settings.api_public_origins == ("https://bot.example.com", "https://alt.example.com:8443") and not settings.warnings
    for bad in ("bot.example.com", "https://bot.example.com/dashboard", "ftp://bot.example.com", "https://user@bot.example.com", "https://", "https://bot.example.com?x=1"):
        with pytest.raises(ConfigError, match="API_PUBLIC_ORIGINS"):
            load({"API_PUBLIC_ORIGINS": bad})
    plain = load({"API_PUBLIC_ORIGINS": "http://bot.example.com"})
    assert plain.api_public_origins == ("http://bot.example.com",) and any("not HTTPS" in w for w in plain.warnings)
