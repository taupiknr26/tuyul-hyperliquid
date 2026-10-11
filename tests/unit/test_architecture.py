"""Architecture and security rules enforced as tests (NFR-011, NFR-016,
SR-06, SR-11, SR-13, PRD §9.6.7)."""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "hlbot"
SDK_MODULE = PACKAGE / "exchange" / "hyperliquid.py"


def sources():
    return sorted(PACKAGE.rglob("*.py"))


def imports(path):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_nfr016_only_the_adapter_imports_the_exchange_sdk():
    offenders = []
    for path in sources():
        if path == SDK_MODULE:
            continue
        for module in imports(path):
            if module.split(".")[0] in ("hyperliquid", "eth_account", "eth_utils", "msgpack"):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders, offenders


def test_nfr016_domain_logic_is_pure_no_io_layers():
    """Strategy, risk and state logic import neither the exchange layer,
    the database, the core, nor any network library."""
    forbidden = ("hlbot.exchange", "hlbot.db", "hlbot.core", "hlbot.api", "hlbot.ingest", "aiohttp", "requests", "telethon", "sqlite3", "asyncio")
    offenders = []
    for path in sorted((PACKAGE / "domain").glob("*.py")):
        for module in imports(path):
            if module.startswith(forbidden):
                offenders.append(f"{path.name} imports {module}")
    assert not offenders, offenders


def test_the_sdk_exchange_class_is_never_used():
    """It bundles transfer and withdrawal methods and draws its own nonces."""
    assert "hyperliquid.exchange" not in imports(SDK_MODULE)
    assert imports(SDK_MODULE) & {"hyperliquid.api", "hyperliquid.utils.signing"} == {"hyperliquid.api", "hyperliquid.utils.signing"}


def test_sr06_no_withdrawal_transfer_agent_approval_or_subaccount_funding_code_path():
    forbidden = re.compile(
        r"usd_transfer|usdSend|withdraw3|withdraw_from_bridge|spot_transfer|spotSend|send_asset|sendAsset|usd_class_transfer|usdClassTransfer|"
        r"approve_agent|approveAgent|sub_account_transfer|subAccountTransfer|sub_account_spot_transfer|vault_usd_transfer|vaultTransfer|"
        r"create_sub_account|approve_builder_fee|token_delegate|sign_user_signed_action|sign_usd|sign_withdraw|sign_agent\b"
    )
    offenders = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in sources() for m in forbidden.finditer(p.read_text())]
    assert not offenders, offenders


def test_the_adapter_builds_only_four_action_types():
    text = SDK_MODULE.read_text()
    literal = set(re.findall(r'\{"type": "(\w+)", "(?:asset|modifies|cancels)"', text))
    assert literal == {"updateLeverage", "batchModify", "cancelByCloid"}
    assert "order_wires_to_order_action" in text  # the fourth: "order"
    assert text.count('"/exchange"') == 1  # a single place posts signed actions


def test_schedule_cancel_is_never_called():
    """§9.6.7: it would cancel the stop-loss and take-profits, the opposite of a safety net."""
    pattern = re.compile(r"schedule_cancel\(|\"scheduleCancel\"|'scheduleCancel'")
    offenders = [str(p.relative_to(ROOT)) for p in sources() if pattern.search(p.read_text())]
    assert not offenders, offenders


def test_implicit_price_helpers_are_not_used():
    """§13.1: market_open / market_close choose a price 5 % through the mid."""
    offenders = [str(p.relative_to(ROOT)) for p in sources() if re.search(r"\.market_open\(|\.market_close\(|_slippage_price\(", p.read_text())]
    assert not offenders, offenders


def test_no_asset_id_is_hard_coded():
    """FR-021: asset ids are resolved from perpDexs + meta at runtime."""
    pattern = re.compile(r"\b11000[0-9]\b|\basset_id\s*=\s*\d{2,}")
    offenders = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p in sources() if p.name != "sim.py" for m in pattern.finditer(p.read_text())]
    assert not offenders, offenders


def test_sr11_gitignore_covers_logs_data_secrets_sessions_and_databases():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    for entry in ("logs/", "data/", ".env", "*.session", "*.session-journal", "*.sqlite3", "*.key", "secrets/"):
        assert entry in lines, entry


def test_sr13_template_and_fixtures_contain_placeholders_only():
    key = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
    token = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
    example = (ROOT / ".env.example").read_text()
    assert not key.search(example) and not token.search(example)
    assert "SECRETS_FILE" in example and "HL_AGENT_KEY_FILE" in example
    for line in example.splitlines():
        if line.startswith(("TELEGRAM_API_HASH=", "TELEGRAM_BOT_TOKEN=", "HL_AGENT_PRIVATE_KEY=")):
            raise AssertionError(f".env.example must not carry secret variables: {line}")
    # Test fixtures build keys from token_hex at run time or use obvious repeated-digit dummies.
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for match in key.finditer(path.read_text()):
            assert len(set(match.group(0).lower().replace("0x", ""))) <= 2, f"{path.name} contains something key-like"
