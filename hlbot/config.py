"""Configuration loading and validation (FR-095, FR-082, SR-03, SR-08).

Non-secret settings come from the process environment and an optional
``.env`` file. Secrets (Telegram API hash, bot token) come from a
separate owner-only file or from the process environment (a container
secret manager), never from ``.env``. The Hyperliquid agent key lives in
its own owner-only file and is read *only* by ``load_agent_key()``,
which paper mode never calls.

Every malformed value stops startup with a message naming the variable.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlsplit

from hlbot.domain.parser import PARSER_FORMATS
from hlbot.domain.risk import RiskConfig

ENVIRONMENTS = ("paper", "testnet", "mainnet")
SECRET_KEYS = ("TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "HL_AGENT_PRIVATE_KEY")
RETIRED_PREFIXES = ("MT5_",)
RETIRED_KEYS = (
    "EXEC_SLIPPAGE_ACTION",
    "EXEC_ORDER_TYPE",
    "EXEC_SL_FAILURE_ACTION",
    "EXEC_BUFFER_UNIT",
    "AUTO_WATCH_INTERVAL",
    "RISK_SIZING_ENABLED",
    "RISK_PERCENT_PER_TRADE",
    "RISK_DAILY_TARGET_PERCENT",
    "RISK_MAX_VOLUME",
    "RISK_MAX_ACCOUNT_RISK_PERCENT",
    "RISK_MAX_CONCURRENT_POSITIONS",
    "RISK_MAX_DAILY_LOSS",
    "RISK_MAX_SYMBOL_VOLUME",
)

REST_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}
WS_URLS = {
    "mainnet": "wss://api.hyperliquid.xyz/ws",
    "testnet": "wss://api.hyperliquid-testnet.xyz/ws",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(Exception):
    """A setting is missing or malformed. The message names the variable."""


class SecretFileError(ConfigError):
    """A secret file is missing or readable by someone other than its owner."""


def short_address(address: Optional[str]) -> Optional[str]:
    """Account addresses appear in logs and output only as a short prefix (NFR-022)."""
    if not address:
        return address
    return address[:8] + "…" if len(address) > 8 else address


def parse_env_file(path: Union[str, Path]) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` reader: ``#`` comment lines, optional
    ``export``, optional matching quotes. No interpolation."""
    values: dict[str, str] = {}
    file = Path(path)
    if not file.exists():
        return values
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_owner_only(path: Path, what: str) -> None:
    """Refuse a secret file that group or others can read (SR-03)."""
    if not path.exists():
        raise SecretFileError(f"{what} not found: {path}")
    if sys.platform == "win32":  # POSIX permission bits are not meaningful there
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SecretFileError(
            f"{what} {path} is readable by group or others (mode {mode:04o}); run: chmod 600 {path}"
        )


def _coerce_channel(value: str) -> Union[str, int]:
    """A bare numeric channel id must reach Telethon as an int; usernames
    and invite links stay strings (ported from the baseline)."""
    stripped = value[1:] if value.startswith("-") else value
    return int(value) if stripped.isdigit() else value


def parse_channels(raw: str) -> list[Union[str, int]]:
    return [_coerce_channel(part.strip()) for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # ---- mode
    env: str = "paper"
    requested_env: str = "paper"
    mode_note: Optional[str] = None
    account_address: Optional[str] = None
    vault_address: Optional[str] = None
    agent_key_file: Optional[str] = None
    symbol_map: dict[str, str] = field(default_factory=lambda: {"GOLD": "xyz:GOLD"})
    margin_mode: str = "cross"
    tp_style: str = "limit"
    rest_timeout_s: float = 5.0
    fee_rate_fallback: float = 0.0009
    paper_start_equity: float = 1000.0
    # ---- execution
    buffer_enabled: bool = True
    buffer_value: float = 2.0
    signal_expiry_minutes: float = 30.0
    tp_close_percents: tuple[float, ...] = (20.0, 20.0, 20.0, 20.0, 20.0)
    tp_buffer_value: float = 0.0
    max_slippage_points: float = 3.0
    min_tp_gap_points: float = 0.5
    duplicate_protection: bool = True
    price_offset_points: float = 0.0
    entry_attempts: int = 3
    entry_retry_min_s: float = 2.0
    entry_ttl_ms: int = 5000
    partial_fill_policy: str = "accept"
    protect_soft_ms: int = 3000
    protect_hard_ms: int = 8000
    flatten_slippage_pct: float = 1.0
    flatten_max_slippage_pct: float = 5.0
    flatten_deadline_ms: int = 15000
    sl_breakeven_after_tp1: bool = False
    sl_resize_on_tp: bool = True
    sl_trigger_slippage_pct: float = 10.0
    ws_stale_ms: int = 10000
    reconcile_interval_s: float = 15.0
    # ---- risk
    risk: RiskConfig = field(default_factory=RiskConfig)
    liq_alert_fraction: Optional[float] = 0.25
    # ---- telegram
    telegram_enabled: bool = True
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    telegram_session: str = "~/.config/hlbot/telegram.session"
    telegram_channels: tuple[Union[str, int], ...] = ()
    telegram_channel_parsers: dict[str, str] = field(default_factory=dict)
    telegram_backfill_limit: int = 50
    notify_enabled: bool = False
    telegram_bot_token: Optional[str] = None
    notify_chat_id: Optional[str] = None
    # ---- runtime
    data_dir: str = "data"
    log_dir: str = "logs"
    log_level: str = "INFO"
    api_enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    # Addresses a reverse proxy or tunnel publishes the dashboard at, e.g. ("https://bot.example.com",)
    api_public_origins: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------ derived
    @property
    def label(self) -> str:
        """Tag attached to every record and notification (FR-080, FR-081)."""
        return self.env.upper()

    @property
    def is_paper(self) -> bool:
        return self.env == "paper"

    @property
    def market_data_env(self) -> str:
        """Paper mode simulates fills against live *mainnet* market data."""
        return "mainnet" if self.env == "paper" else self.env

    @property
    def rest_url(self) -> str:
        return REST_URLS[self.market_data_env]

    @property
    def ws_url(self) -> str:
        return WS_URLS[self.market_data_env]

    @property
    def account_key(self) -> str:
        """Identity used in cloids and the instance lock."""
        return (self.vault_address or self.account_address or "paper").lower()

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / f"hlbot-{self.env}.sqlite3"

    @property
    def lock_path(self) -> Path:
        digest = hashlib.sha256(f"{self.env}|{self.account_key}".encode()).hexdigest()[:12]
        return self.data_path / f"hlbot-{self.env}-{digest}.lock"

    @property
    def stop_flag_path(self) -> Path:
        return self.data_path / "stop.flag"

    @property
    def pid_path(self) -> Path:
        return self.data_path / "hlbot.pid"

    @property
    def control_token_path(self) -> Path:
        return self.data_path / "control.token"

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir) / "hlbot.log"

    @property
    def session_path(self) -> Path:
        return Path(os.path.expanduser(self.telegram_session))

    @property
    def coins(self) -> tuple[str, ...]:
        """Managed Hyperliquid coins, de-duplicated, in mapping order."""
        return tuple(dict.fromkeys(self.symbol_map.values()))

    def map_symbol(self, symbol: Optional[str]) -> Optional[str]:
        return self.symbol_map.get(str(symbol or "").upper())

    def secret_values(self) -> list[str]:
        return [v for v in (self.telegram_api_hash, self.telegram_bot_token) if v]

    def public_dict(self) -> dict[str, Any]:
        """Resolved settings with secrets removed and addresses shortened;
        this is what ``botctl config`` prints and what is stored as a
        config version (§12.1 TradingConfig)."""
        data = asdict(self)
        data["telegram_api_hash"] = "***" if self.telegram_api_hash else None
        data["telegram_bot_token"] = "***" if self.telegram_bot_token else None
        data["notify_chat_id"] = "***" if self.notify_chat_id else None
        data["account_address"] = short_address(self.account_address)
        data["vault_address"] = short_address(self.vault_address)
        data["telegram_channels"] = [str(c) for c in self.telegram_channels]
        data["tp_close_percents"] = list(self.tp_close_percents)
        data.pop("warnings", None)
        return data

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.public_dict(), sort_keys=True).encode()).hexdigest()[:16]


class _Reader:
    """Typed access to raw string settings; collects every error."""

    def __init__(self, values: Mapping[str, str]):
        self.values = values
        self.errors: list[str] = []

    def raw(self, key: str) -> Optional[str]:
        value = self.values.get(key)
        if value is None:
            return None
        value = value.strip()
        return value if value != "" else None

    def text(self, key: str, default: Optional[str] = None, choices: Optional[tuple[str, ...]] = None):
        value = self.raw(key)
        if value is None:
            return default
        if choices and value.lower() not in choices:
            self.errors.append(f"{key} must be one of {', '.join(choices)} (got {value!r})")
            return default
        return value.lower() if choices else value

    def boolean(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        if value.lower() in _TRUE:
            return True
        if value.lower() in _FALSE:
            return False
        self.errors.append(f"{key} must be true or false (got {value!r})")
        return default

    def _number(
        self,
        key: str,
        default,
        cast: Callable,
        kind: str,
        minimum=None,
        maximum=None,
        exclusive_min: bool = False,
    ):
        value = self.raw(key)
        if value is None:
            return default
        try:
            number = cast(value)
        except ValueError:
            self.errors.append(f"{key} must be {kind} (got {value!r})")
            return default
        if number != number or number in (float("inf"), float("-inf")):
            self.errors.append(f"{key} must be a finite number")
            return default
        if minimum is not None and (number <= minimum if exclusive_min else number < minimum):
            self.errors.append(f"{key} must be {'>' if exclusive_min else '>='} {minimum} (got {value})")
            return default
        if maximum is not None and number > maximum:
            self.errors.append(f"{key} must be <= {maximum} (got {value})")
            return default
        return number

    def number(self, key, default, minimum=None, maximum=None, exclusive_min=False):
        return self._number(key, default, float, "a number", minimum, maximum, exclusive_min)

    def integer(self, key, default, minimum=None, maximum=None):
        return self._number(key, default, int, "an integer", minimum, maximum)

    def origins(self, key: str) -> tuple[str, ...]:
        """Comma-separated web origins: scheme and host only, no path."""
        found: list[str] = []
        for item in (self.raw(key) or "").split(","):
            item = item.strip().rstrip("/").lower()
            if not item:
                continue
            parts = urlsplit(item)
            if parts.scheme not in ("https", "http") or not parts.hostname or "@" in parts.netloc or parts.path or parts.query or parts.fragment:
                self.errors.append(f"{key}: {item!r} must look like https://dashboard.example.com (scheme and host only)")
                continue
            found.append(item)
        return tuple(dict.fromkeys(found))

    def json_object(self, key: str, default: dict[str, str]) -> dict[str, str]:
        value = self.raw(key)
        if value is None:
            return dict(default)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            self.errors.append(f"{key} must be a JSON object, e.g. {{\"GOLD\": \"xyz:GOLD\"}}")
            return dict(default)
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in parsed.items()
        ):
            self.errors.append(f"{key} must be a JSON object of string to string")
            return dict(default)
        return parsed

    def address(self, key: str) -> Optional[str]:
        value = self.raw(key)
        if value is None:
            return None
        body = value[2:] if value.lower().startswith("0x") else ""
        if len(body) != 40 or any(c not in "0123456789abcdefABCDEF" for c in body):
            self.errors.append(f"{key} must be a 0x-prefixed 40-hex-character address")
            return None
        return value.lower()


def load_settings(
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Union[str, Path]] = ".env",
    mainnet_flag: bool = False,
) -> Settings:
    """Build validated settings. ``mainnet_flag`` is the explicit command
    line flag; mainnet needs it *and* a matching ``HL_MAINNET_CONFIRM``
    (FR-082). Without both, the process runs in paper mode and says so."""
    process_env = dict(os.environ if environ is None else environ)
    file_values = parse_env_file(env_file) if env_file else {}

    leaked = sorted(k for k in SECRET_KEYS if file_values.get(k, "").strip())
    if leaked:
        raise ConfigError(
            f"{', '.join(leaked)} must not be stored in {env_file}; move it to the owner-only "
            "secrets file named by SECRETS_FILE (see .env.example)"
        )

    values: dict[str, str] = {**file_values, **process_env}
    r = _Reader(values)
    warnings: list[str] = []

    for key in sorted(values):
        if (key in RETIRED_KEYS or key.startswith(RETIRED_PREFIXES)) and r.raw(key) is not None:
            warnings.append(f"{key} is retired and ignored (see README: migrating from the MT5 bot)")

    # ---- secrets
    secrets: dict[str, str] = {}
    secrets_file = r.raw("SECRETS_FILE")
    if secrets_file:
        path = Path(os.path.expanduser(secrets_file))
        check_owner_only(path, "secrets file")
        secrets = parse_env_file(path)
    for key in ("TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"):
        if process_env.get(key, "").strip():
            secrets[key] = process_env[key].strip()
    if "HL_AGENT_PRIVATE_KEY" in secrets or process_env.get("HL_AGENT_PRIVATE_KEY"):
        raise ConfigError(
            "HL_AGENT_PRIVATE_KEY is not read from the environment or the secrets file; "
            "put the agent key alone in an owner-only file and set HL_AGENT_KEY_FILE"
        )

    # ---- mode (FR-080..FR-082)
    requested_env = r.text("HL_ENV", "paper", ENVIRONMENTS) or "paper"
    account_address = r.address("HL_ACCOUNT_ADDRESS")
    vault_address = r.address("HL_VAULT_ADDRESS")
    agent_key_file = r.raw("HL_AGENT_KEY_FILE")
    env = requested_env
    mode_note: Optional[str] = None
    if requested_env == "mainnet":
        confirm = (r.raw("HL_MAINNET_CONFIRM") or "").lower()
        if not mainnet_flag:
            env = "paper"
            mode_note = "HL_ENV=mainnet but the --mainnet flag was not given: running in PAPER mode"
        elif not account_address or confirm != account_address:
            env = "paper"
            mode_note = (
                "HL_ENV=mainnet but HL_MAINNET_CONFIRM does not match HL_ACCOUNT_ADDRESS: "
                "running in PAPER mode"
            )
    if env in ("testnet", "mainnet"):
        if not account_address:
            r.errors.append(f"HL_ACCOUNT_ADDRESS is required when HL_ENV={env}")
        if not agent_key_file:
            r.errors.append(f"HL_AGENT_KEY_FILE is required when HL_ENV={env}")

    symbol_map = {k.upper(): v for k, v in r.json_object("HL_SYMBOL_MAP", {"GOLD": "xyz:GOLD"}).items()}
    if not symbol_map:
        r.errors.append("HL_SYMBOL_MAP must map at least one signal symbol to a Hyperliquid coin")

    # ---- execution
    percents_raw = r.raw("EXEC_TP_CLOSE_PERCENTS") or "20,20,20,20,20"
    try:
        percents = tuple(float(p) for p in percents_raw.split(",") if p.strip())
    except ValueError:
        percents = ()
    if not percents or any(p <= 0 for p in percents) or abs(sum(percents) - 100.0) > 1e-6:
        r.errors.append("EXEC_TP_CLOSE_PERCENTS must be positive numbers that add up to 100")
        percents = (100.0,)

    duplicate_protection = r.boolean("EXEC_DUPLICATE_PROTECTION", True)
    if env == "mainnet" and not duplicate_protection:
        r.errors.append("EXEC_DUPLICATE_PROTECTION cannot be disabled on mainnet")

    protect_soft_ms = r.integer("EXEC_PROTECT_SOFT_MS", 3000, minimum=100)
    protect_hard_ms = r.integer("EXEC_PROTECT_HARD_MS", 8000, minimum=200)
    if protect_hard_ms <= protect_soft_ms:
        r.errors.append("EXEC_PROTECT_HARD_MS must be greater than EXEC_PROTECT_SOFT_MS")
    flatten_slippage = r.number("EXEC_FLATTEN_SLIPPAGE_PCT", 1.0, minimum=0, exclusive_min=True, maximum=50)
    flatten_max_slippage = r.number("EXEC_FLATTEN_MAX_SLIPPAGE_PCT", 5.0, minimum=0, exclusive_min=True, maximum=50)
    if flatten_max_slippage < flatten_slippage:
        r.errors.append("EXEC_FLATTEN_MAX_SLIPPAGE_PCT must be >= EXEC_FLATTEN_SLIPPAGE_PCT")

    # ---- risk (hard bounds from PRD §10.6)
    sizing_mode = r.text("RISK_SIZING_MODE", "risk", ("risk", "fixed")) or "risk"
    fixed_size = r.number("RISK_FIXED_SIZE", None, minimum=0, exclusive_min=True)
    if sizing_mode == "fixed" and fixed_size is None:
        r.errors.append("RISK_FIXED_SIZE is required when RISK_SIZING_MODE=fixed")
    compound = r.boolean("RISK_COMPOUND_ENABLED", True)
    baseline = r.number("RISK_FIXED_EQUITY_BASELINE", None, minimum=0, exclusive_min=True)
    if not compound and baseline is None:
        r.errors.append("RISK_FIXED_EQUITY_BASELINE is required when RISK_COMPOUND_ENABLED=false")
    trades_per_asset = r.integer("RISK_MAX_TRADES_PER_ASSET", 1, minimum=1)
    if trades_per_asset != 1:
        r.errors.append("RISK_MAX_TRADES_PER_ASSET is fixed at 1 in v1 (one net position per asset)")
    margin_mode = r.text("HL_MARGIN_MODE", "cross", ("cross", "isolated")) or "cross"
    risk = RiskConfig(
        risk_per_trade_pct=r.number("RISK_PER_TRADE_PCT", 0.5, minimum=0, exclusive_min=True, maximum=5),
        sizing_mode=sizing_mode,
        fixed_size=fixed_size,
        compound_enabled=compound,
        fixed_equity_baseline=baseline,
        max_daily_loss_pct=r.number("RISK_MAX_DAILY_LOSS_PCT", 2.0, minimum=0, exclusive_min=True, maximum=20),
        daily_target_pct=r.number("RISK_DAILY_TARGET_PCT", None, minimum=0, exclusive_min=True),
        max_open_trades=r.integer("RISK_MAX_OPEN_TRADES", 3, minimum=1),
        max_trades_per_asset=1,
        max_exposure_x_equity=r.number("RISK_MAX_EXPOSURE_X_EQUITY", 3.0, minimum=0, exclusive_min=True, maximum=50),
        leverage=r.integer("RISK_LEVERAGE", 5, minimum=1, maximum=50),
        min_rr=r.number("RISK_MIN_RR", 0.5, minimum=0),
        sl_slippage_allowance_points=r.number("RISK_SL_SLIPPAGE_ALLOWANCE_POINTS", 1.0, minimum=0),
        max_sl_distance=r.number("RISK_MAX_SL_DISTANCE", 30.0, minimum=0, exclusive_min=True),
        margin_usage_cap=r.number("RISK_MARGIN_USAGE_CAP", 0.5, minimum=0, exclusive_min=True, maximum=0.9),
        liq_buffer_x=r.number("RISK_LIQ_BUFFER_X", 1.5, minimum=1.1),
        max_mark_oracle_dev_points=r.number("RISK_MAX_MARK_ORACLE_DEV_POINTS", None, minimum=0, exclusive_min=True),
        max_trade_notional=r.number("RISK_MAX_TRADE_NOTIONAL", 5000.0, minimum=0, exclusive_min=True),
        max_quote_age_ms=r.integer("EXEC_MAX_QUOTE_AGE_MS", 2000, minimum=100),
        max_account_age_ms=r.integer("EXEC_MAX_ACCOUNT_AGE_MS", 5000, minimum=100),
        margin_mode=margin_mode,
    )

    # ---- telegram
    telegram_enabled = r.boolean("TELEGRAM_ENABLED", True)
    api_id = r.integer("TELEGRAM_API_ID", None)
    api_hash = secrets.get("TELEGRAM_API_HASH") or None
    channels = tuple(parse_channels(r.raw("TELEGRAM_CHANNEL") or ""))
    if telegram_enabled:
        if api_id is None:
            r.errors.append("TELEGRAM_API_ID is required (or set TELEGRAM_ENABLED=false)")
        if not api_hash:
            r.errors.append("TELEGRAM_API_HASH is required in the secrets file (or set TELEGRAM_ENABLED=false)")
        if not channels:
            r.errors.append("TELEGRAM_CHANNEL must contain at least one channel (username, id, or invite link)")
    channel_parsers = r.json_object("TELEGRAM_CHANNEL_PARSERS", {})
    for channel, fmt in channel_parsers.items():
        if fmt not in PARSER_FORMATS:
            r.errors.append(
                f"TELEGRAM_CHANNEL_PARSERS: unknown format {fmt!r} for {channel!r} "
                f"(known: {', '.join(sorted(PARSER_FORMATS))})"
            )
    notify_enabled = r.boolean("TELEGRAM_NOTIFY_ENABLED", False)
    bot_token = secrets.get("TELEGRAM_BOT_TOKEN") or None
    chat_id = r.raw("TELEGRAM_NOTIFY_CHAT_ID")
    if notify_enabled and not bot_token:
        r.errors.append("TELEGRAM_NOTIFY_ENABLED is true but TELEGRAM_BOT_TOKEN is not in the secrets file")
    if notify_enabled and not chat_id:
        r.errors.append("TELEGRAM_NOTIFY_ENABLED is true but TELEGRAM_NOTIFY_CHAT_ID is not set")

    public_origins = r.origins("API_PUBLIC_ORIGINS")
    for origin in public_origins:
        if origin.startswith("http://"):
            warnings.append(f"API_PUBLIC_ORIGINS: {origin} is not HTTPS; passwords and the sign-in cookie would cross the network unencrypted")
    settings = Settings(
        env=env,
        requested_env=requested_env,
        mode_note=mode_note,
        account_address=account_address,
        vault_address=vault_address,
        agent_key_file=agent_key_file,
        symbol_map=symbol_map,
        margin_mode=margin_mode,
        tp_style=r.text("HL_TP_STYLE", "limit", ("limit", "trigger")) or "limit",
        rest_timeout_s=r.number("HL_REST_TIMEOUT_S", 5.0, minimum=0.5, maximum=60),
        fee_rate_fallback=r.number("HL_FEE_RATE_FALLBACK", 0.0009, minimum=0, maximum=0.01),
        paper_start_equity=r.number("PAPER_START_EQUITY", 1000.0, minimum=0, exclusive_min=True),
        buffer_enabled=r.boolean("EXEC_BUFFER_ENABLED", True),
        buffer_value=r.number("EXEC_BUFFER_VALUE", 2.0, minimum=0),
        signal_expiry_minutes=r.number("EXEC_SIGNAL_EXPIRY_MINUTES", 30.0, minimum=0, exclusive_min=True),
        tp_close_percents=percents,
        tp_buffer_value=r.number("EXEC_TP_BUFFER_VALUE", 0.0, minimum=0),
        max_slippage_points=r.number("EXEC_MAX_SLIPPAGE_POINTS", 3.0, minimum=0),
        min_tp_gap_points=r.number("EXEC_MIN_TP_GAP_POINTS", 0.5, minimum=0),
        duplicate_protection=duplicate_protection,
        price_offset_points=r.number("EXEC_PRICE_OFFSET_POINTS", 0.0),
        entry_attempts=r.integer("EXEC_ENTRY_ATTEMPTS", 3, minimum=1, maximum=10),
        entry_retry_min_s=r.number("EXEC_ENTRY_RETRY_MIN_S", 2.0, minimum=0),
        entry_ttl_ms=r.integer("EXEC_ENTRY_TTL_MS", 5000, minimum=500, maximum=60000),
        partial_fill_policy=r.text("EXEC_PARTIAL_FILL_POLICY", "accept", ("accept", "flatten")) or "accept",
        protect_soft_ms=protect_soft_ms,
        protect_hard_ms=protect_hard_ms,
        flatten_slippage_pct=flatten_slippage,
        flatten_max_slippage_pct=flatten_max_slippage,
        flatten_deadline_ms=r.integer("EXEC_FLATTEN_DEADLINE_MS", 15000, minimum=500),
        sl_breakeven_after_tp1=r.boolean("EXEC_SL_BREAKEVEN_AFTER_TP1", False),
        sl_resize_on_tp=r.boolean("EXEC_SL_RESIZE_ON_TP", True),
        sl_trigger_slippage_pct=r.number("EXEC_SL_TRIGGER_SLIPPAGE_PCT", 10.0, minimum=0, exclusive_min=True, maximum=50),
        ws_stale_ms=r.integer("EXEC_WS_STALE_MS", 10000, minimum=1000),
        reconcile_interval_s=r.number("AUTO_POLL_INTERVAL", 15.0, minimum=1, maximum=300),
        risk=risk,
        liq_alert_fraction=r.number("RISK_LIQ_ALERT_FRACTION", 0.25, minimum=0, exclusive_min=True, maximum=1),
        telegram_enabled=telegram_enabled,
        telegram_api_id=api_id,
        telegram_api_hash=api_hash,
        telegram_session=r.raw("TELEGRAM_SESSION") or "~/.config/hlbot/telegram.session",
        telegram_channels=channels,
        telegram_channel_parsers=channel_parsers,
        telegram_backfill_limit=r.integer("TELEGRAM_BACKFILL_LIMIT", 50, minimum=0, maximum=1000),
        notify_enabled=notify_enabled,
        telegram_bot_token=bot_token,
        notify_chat_id=chat_id,
        data_dir=r.raw("DATA_DIR") or "data",
        log_dir=r.raw("LOG_DIR") or "logs",
        log_level=(r.text("LOG_LEVEL", "info", ("debug", "info", "warning", "error")) or "info").upper(),
        api_enabled=r.boolean("API_ENABLED", True),
        api_host=r.raw("API_HOST") or "127.0.0.1",
        api_port=r.integer("API_PORT", 8787, minimum=1, maximum=65535),
        api_public_origins=public_origins,
        warnings=tuple(warnings),
    )
    if r.errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(r.errors))
    return settings


def load_agent_key(settings: Settings) -> str:
    """Read the Hyperliquid agent (API wallet) key. Never called in paper
    mode (FR-080, SR-08): the paper process does not open the key file."""
    if settings.is_paper:
        raise ConfigError("paper mode never loads a signing key")
    if not settings.agent_key_file:
        raise ConfigError("HL_AGENT_KEY_FILE is not set")
    path = Path(os.path.expanduser(settings.agent_key_file))
    check_owner_only(path, "agent key file")
    key = path.read_text(encoding="utf-8").strip()
    body = key[2:] if key.lower().startswith("0x") else key
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ConfigError("HL_AGENT_KEY_FILE does not contain a 32-byte hex private key")
    return "0x" + body.lower()
