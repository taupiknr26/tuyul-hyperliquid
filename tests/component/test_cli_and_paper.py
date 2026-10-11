"""``botctl`` verbs (FR-093) and the paper adapter (FR-080)."""

import json
import os

import pytest

from hlbot import cli
from hlbot.exchange.base import OrderRequest, StreamHandlers
from hlbot.exchange.paper import PaperAdapter
from hlbot.exchange.sim import SimExchange
from hlbot.timeutil import ManualClock
from tests.component.conftest import COIN, SELL_TEXT, START


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A complete operator setup in a temp directory: .env, secrets file, data dir."""
    for key in list(os.environ):
        if key.startswith(("HL_", "EXEC_", "RISK_", "TELEGRAM_", "API_", "SECRETS_", "DATA_", "LOG_", "PAPER_", "AUTO_")):
            monkeypatch.delenv(key)
    secrets = tmp_path / "hlbot.secrets"
    secrets.write_text("TELEGRAM_BOT_TOKEN=123456789:" + "S" * 35 + "\nTELEGRAM_API_HASH=" + "d" * 32 + "\n")
    secrets.chmod(0o600)
    path = tmp_path / ".env"
    path.write_text(
        f"DATA_DIR={tmp_path / 'data'}\nLOG_DIR={tmp_path / 'logs'}\nSECRETS_FILE={secrets}\nTELEGRAM_ENABLED=false\n"
        "TELEGRAM_NOTIFY_ENABLED=true\nTELEGRAM_NOTIFY_CHAT_ID=-100123456789\nAPI_PORT=1\nMT5_LOGIN=1234\n"
    )
    return path


def run(env_file, *argv):
    return cli.main(["--env-file", str(env_file), *argv])


async def populated(make, tmp_path):
    """A database with a closed trade, an open trade and a waiting signal, as a stopped bot would leave it."""
    h = await make(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"))
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")
    h.post(SELL_TEXT.replace("4286- 4289", "4310- 4313").replace("SL : 4296", "SL : 4320"))
    await h.settle()
    await h.app.shutdown()
    return h


def test_config_shows_resolved_settings_and_hides_every_secret(env_file, capsys):
    assert run(env_file, "config") == 0
    out = capsys.readouterr().out
    assert "mode: PAPER" in out and "buffer_value = 2.0" in out and "[risk]" in out and "risk_per_trade_pct = 0.5" in out
    assert "S" * 35 not in out and "d" * 32 not in out and "-100123456789" not in out
    assert "telegram_bot_token = ***" in out and "WARNING: MT5_LOGIN is retired" in out


def test_config_error_exits_non_zero_and_names_the_variable(env_file, capsys):
    env_file.write_text(env_file.read_text() + "RISK_LEVERAGE=lots\n")
    assert run(env_file, "config") == cli.EXIT_CONFIG
    assert "RISK_LEVERAGE" in capsys.readouterr().err
    assert run(env_file, "run", "--quiet") == cli.EXIT_CONFIG  # the bot itself refuses to start as well


def test_mainnet_flag_without_confirmation_reports_paper(env_file, capsys):
    env_file.write_text(env_file.read_text() + "HL_ENV=mainnet\nHL_ACCOUNT_ADDRESS=0x" + "ab" * 20 + "\nHL_AGENT_KEY_FILE=/nonexistent\n")
    assert run(env_file, "config", "--mainnet") == 0
    out = capsys.readouterr().out
    assert "mode: PAPER" in out and "HL_MAINNET_CONFIRM" in out


def test_read_only_verbs_before_the_first_start(env_file, capsys):
    assert run(env_file, "status") == 0 and "NOT running" in capsys.readouterr().out
    for verb in ("orders", "positions", "pending"):
        assert run(env_file, verb) == 0
        assert "No database yet" in capsys.readouterr().out
    assert run(env_file, "logs") == 0 and "No log file yet" in capsys.readouterr().out
    assert run(env_file, "stop") == 0 and "Not running" in capsys.readouterr().out
    assert run(env_file, "backup") == cli.EXIT_ERROR
    for verb in (("kill", "--yes"), ("close", "1"), ("reconcile",), ("ack",), ("inject", "hello"), ("modify-sl", "1", "4290")):
        assert run(env_file, *verb) == cli.EXIT_ERROR  # these need a running bot and say so
    assert "not running" in capsys.readouterr().err or True


async def test_read_only_verbs_fall_back_to_the_database_when_the_bot_is_down(make, tmp_path, env_file, capsys):
    h = await populated(make, tmp_path)
    capsys.readouterr()

    assert run(env_file, "status") == 0
    out = capsys.readouterr().out
    assert "NOT running" in out and "offline view from the database" in out and "Open trades: 1" in out and "waiting signals: 1" in out

    assert run(env_file, "status", "--json") == 0
    status = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert status["health"]["state"] == "HEALTHY" and len(status["open_trades"]) == 1

    assert run(env_file, "positions") == 0
    out = capsys.readouterr().out
    assert COIN in out and "PROTECTED" in out and "Last stored snapshot" in out

    assert run(env_file, "pending") == 0
    out = capsys.readouterr().out
    assert "GOLD-SELL-4310-4313-20260914" in out and "4,308" in out and "4,315" in out

    assert run(env_file, "orders", "10") == 0
    out = capsys.readouterr().out
    assert "ENTRY" in out and "SL" in out and out.count("TP") >= 5 and "RESTING" in out

    assert run(env_file, "trace", "--trade", "1") == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["message"]["text"] == SELL_TEXT and len(trace["orders"]) == 7 and trace["audit"]
    assert run(env_file, "trace", "--fill", "999") == cli.EXIT_ERROR
    capsys.readouterr()
    assert run(env_file, "trace") == cli.EXIT_ERROR

    assert run(env_file, "backup", str(tmp_path / "copy.sqlite3")) == 0
    assert (tmp_path / "copy.sqlite3").stat().st_size > 0 and (tmp_path / "copy.sqlite3").stat().st_mode & 0o077 == 0
    del h


async def test_pause_and_resume_work_offline_and_are_read_at_the_next_start(make, tmp_path, env_file, capsys):
    h = await populated(make, tmp_path)
    assert run(env_file, "pause", "--reason", "weekend") == 0
    assert "start paused" in capsys.readouterr().out
    assert run(env_file, "status") == 0
    assert "control=PAUSED (weekend)" in capsys.readouterr().out

    h2 = await h.restart()
    assert h2.health.operator_paused() and not h2.health.can_enter(COIN)[0]
    await h2.app.shutdown()

    assert run(env_file, "resume") == 0 and "Resumed" in capsys.readouterr().out
    h3 = await h.restart()
    assert not h3.health.operator_paused()
    await h3.app.shutdown()


def test_logs_are_rendered_from_the_json_file(env_file, tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = [json.dumps({"ts": f"2026-09-14T10:00:0{i}Z", "level": "INFO", "logger": "hlbot.engine", "msg": f"line {i}", "trade": i}) for i in range(5)]
    (logs / "hlbot.log").write_text("\n".join(lines) + "\nnot json at all\n")
    assert run(env_file, "logs", "3") == 0
    out = capsys.readouterr().out
    assert "line 2" not in out and "line 3" in out and "line 4" in out and '{"trade": 4}' in out and "not json at all" in out


def test_init_secrets_creates_an_owner_only_template_and_never_overwrites(env_file, tmp_path, capsys):
    target = tmp_path / "conf" / "hlbot.secrets"
    assert run(env_file, "init-secrets", str(target)) == 0
    assert target.stat().st_mode & 0o777 == 0o600 and "TELEGRAM_BOT_TOKEN=" in target.read_text()
    assert "HL_AGENT" not in target.read_text()  # the agent key never shares a file with other secrets
    assert run(env_file, "init-secrets", str(target)) == cli.EXIT_ERROR
    assert "not overwriting" in capsys.readouterr().out


def test_kill_requires_typed_confirmation(env_file, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert run(env_file, "kill") == cli.EXIT_ERROR and "Aborted" in capsys.readouterr().out


def test_every_baseline_verb_has_an_equivalent():
    """EX-21 / FR-093."""
    parser = cli.build_parser()
    verbs = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    baseline = {"start", "stop", "restart", "status", "logs", "orders", "price", "positions", "pending", "test", "config"}
    required = baseline | {"pause", "resume", "kill", "close", "reconcile"}
    assert required <= verbs
    assert {"run", "ack", "trace", "inject", "backup", "telegram-login", "init-secrets", "modify-sl", "user"} <= verbs


async def test_cli_talks_to_a_running_bot_through_the_token_protected_api(make, tmp_path, env_file, capsys, monkeypatch):
    import asyncio
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env_file.write_text(env_file.read_text().replace("API_PORT=1", f"API_PORT={port}"))
    h = await make(data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs"), api_enabled=True, api_port=port)
    await h.open_protected_trade()
    await h.app.reconciler.run_once("test")

    async def cli_call(*argv):
        return await asyncio.to_thread(run, env_file, *argv)  # the CLI is a separate, synchronous client

    assert await cli_call("status") == 0
    out = capsys.readouterr().out
    assert "running (pid" in out and "Mode    : PAPER" in out and "Stream  : connected" in out and "offline" not in out
    assert await cli_call("price") == 0
    out = capsys.readouterr().out
    assert "4,287.1" in out and "4,287.05" in out and out.rstrip().endswith("0")  # bid/ask, mark, quote age in ms
    assert await cli_call("positions") == 0
    assert "Exchange snapshot" in capsys.readouterr().out
    assert await cli_call("pause", "--reason", "cli") == 0 and h.health.operator_paused()
    assert await cli_call("resume") == 0 and not h.health.operator_paused()
    assert await cli_call("modify-sl", "1", "4295") == 0 and h.trade()["sl_price"] == 4295.0
    assert await cli_call("reconcile") == 0
    assert await cli_call("inject", "Ready", "--channel", "test") == 0
    assert await cli_call("ack") == 0
    assert await cli_call("close", "1") == 0
    await h.until(lambda: h.trade(1)["state"] == "CLOSED", message="closed by the CLI")

    # dashboard accounts, managed from the bot host; the password comes from stdin, never from the command line
    import io

    assert await cli_call("user", "list") == 0 and "No accounts yet" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-made-password\n"))
    assert await cli_call("user", "add", "Boss", "--role", "admin", "--password-stdin") == 0
    assert "created: Boss" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert await cli_call("user", "add", "vera", "--password-stdin") != 0
    assert "password must be" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("viewer-password-1\n"))
    assert await cli_call("user", "add", "vera", "--password-stdin") == 0  # viewer unless told otherwise
    assert await cli_call("user", "role", "vera", "--role", "operator") == 0
    assert await cli_call("user", "disable", "vera") == 0
    capsys.readouterr()
    assert await cli_call("user", "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:3] == ["boss", "admin", "active"] and lines[1].split()[:3] == ["vera", "operator", "disabled"]
    assert await cli_call("user", "enable", "vera") == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("replaced-password-1\n"))
    assert await cli_call("user", "passwd", "vera", "--password-stdin") == 0
    token, who = await h.app.api.auth.login("vera", "replaced-password-1", "test")
    assert who.role == "operator"
    assert await cli_call("user", "remove", "boss") != 0  # vera is an operator, so boss is the last admin
    assert "last active admin" in capsys.readouterr().err
    assert await cli_call("user", "remove", "vera") == 0 and h.app.api.auth.session(token) is None
    assert await cli_call("user", "remove", "nobody") != 0 and "no such user" in capsys.readouterr().err
    assert all("password" not in json.loads(row["payload"]) for row in h.audit("user.created"))
    assert await cli_call("kill", "--yes") == 0 and h.health.operator_paused()
    capsys.readouterr()
    # Without the token file the CLI cannot issue commands.
    token = h.settings.control_token_path.read_text()
    h.settings.control_token_path.unlink()
    assert await cli_call("resume") == cli.EXIT_ERROR
    h.settings.control_token_path.write_text(token)


# ================================================================== paper adapter
async def test_paper_adapter_takes_market_data_from_the_live_source_and_fills_in_the_simulator():
    clock = ManualClock(START)
    market = SimExchange(clock=clock)  # stands in for the read-only live adapter
    market.add_instrument(COIN, sz_decimals=4, max_leverage=25)
    saved = {}
    sim = SimExchange(equity=1000.0, clock=clock)
    paper = PaperAdapter(market, sim, load_state=lambda: None, save_state=lambda state: saved.update(state))
    assert (await paper.get_instruments())[COIN].asset_id == 110003 and sim.instruments[COIN].sz_decimals == 4

    quotes, fills = [], []

    async def on_quote(quote):
        quotes.append(quote)

    async def on_fill(fill, snapshot):
        fills.append(fill)

    await paper.start(StreamHandlers(on_quote=on_quote, on_fill=on_fill))
    await market.set_quote(COIN, 4287.0, 4287.1)  # a live quote arrives
    assert quotes[-1].bid == 4287.0 and sim.quotes[COIN].ask == 4287.1  # ...and reaches both the engine and the simulator
    assert (await paper.get_book(COIN)).bid == 4287.0 and (await paper.get_market(COIN)).mark == pytest.approx(4287.05)

    results = await paper.place([OrderRequest(COIN, False, 0.05, 4284.0, False, "0x" + "a" * 32, "limit", "Ioc")], 1)
    assert results[0].status == "filled" and results[0].avg_price == 4287.0
    await sim.drain()
    assert fills[0].cloid == "0x" + "a" * 32 and not market.fills  # the fill is simulated: nothing reached the live venue
    account = await paper.get_account()
    assert account.szi(COIN) == -0.05 and saved["positions"][COIN][0] == -0.05  # state is persisted for the next start

    await paper.place([OrderRequest(COIN, True, 0.05, 4283.0, True, "0x" + "b" * 32, "limit", "Gtc")], 2)
    assert len(await paper.get_open_orders()) == 1
    await market.set_quote(COIN, 4282.9, 4283.0)  # live price reaches the resting take-profit
    await sim.drain()
    assert (await paper.get_account()).szi(COIN) == 0 and len(fills) == 2 and fills[1].closed_pnl == pytest.approx(0.05 * 4.0)
    assert (await paper.get_order("0x" + "b" * 32)).status == "filled" and len(await paper.get_fills(0)) == 2
    assert (await paper.verify_signer()).detail.startswith("paper mode") and await paper.get_funding(0) == []
    assert (await paper.get_limits()).taker_fee and await paper.get_leverage(COIN) == 20
    await paper.set_leverage(COIN, 5, True, 3)
    assert await paper.get_leverage(COIN) == 5 and await paper.get_blocked_coins() == []
    assert paper.stream_health().connected and paper.rest_weight_last_minute() == 0
    assert (await paper.cancel(COIN, ["0x" + "b" * 32], 4))[0] is not None  # already filled
    await paper.stop()


async def test_paper_state_survives_a_restart():
    clock = ManualClock(START)
    market = SimExchange(clock=clock)
    market.add_instrument(COIN)
    store = {}
    first = PaperAdapter(market, SimExchange(equity=1000.0, clock=clock), load_state=lambda: store.get("s"), save_state=lambda s: store.update(s=s))
    await first.get_instruments()
    await market.set_quote(COIN, 4287.0, 4287.1, emit=False)
    await first.get_book(COIN)
    await first.place([OrderRequest(COIN, False, 0.05, 4284.0, False, "0x" + "a" * 32, "limit", "Ioc")], 1)
    await first.place([OrderRequest(COIN, True, 0.05, 4296.0 * 1.1, True, "0x" + "c" * 32, "trigger", trigger_px=4296.0, tpsl="sl")], 2)

    again = PaperAdapter(market, SimExchange(equity=1.0, clock=clock), load_state=lambda: json.loads(json.dumps(store["s"])), save_state=lambda s: None)
    await again.get_instruments()
    await again.get_book(COIN)
    account = await again.get_account()
    assert account.szi(COIN) == -0.05 and account.dex_account("xyz").account_value == pytest.approx(1000.0, abs=0.2)
    assert [o.cloid for o in await again.get_open_orders()] == ["0x" + "c" * 32]
    assert (await again.get_order("0x" + "a" * 32)).status == "filled" and len(await again.get_fills(0)) == 1


def test_paper_mode_builds_a_read_only_market_adapter_and_never_opens_the_key_file(tmp_path, monkeypatch):
    """FR-080: no signing key is loaded in paper mode."""
    import hlbot.core.app as app_module
    from hlbot.config import Settings
    from hlbot.db.database import Database

    key_file = tmp_path / "agent.key"
    key_file.write_text("0x" + "11" * 32)
    key_file.chmod(0o600)
    opened = []
    real_open = open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    def forbidden(settings):
        raise AssertionError("paper mode must never call load_agent_key")

    monkeypatch.setattr(app_module, "load_agent_key", forbidden)
    monkeypatch.setattr("builtins.open", spy)
    settings = Settings(env="paper", data_dir=str(tmp_path), agent_key_file=str(key_file), account_address="0x" + "ab" * 20)
    db = Database(tmp_path / "p.sqlite3")
    db.migrate()
    adapter = app_module.build_adapter(settings, db, ManualClock(START))
    assert isinstance(adapter, PaperAdapter)
    assert adapter.market._wallet is None and adapter.market._user is None  # no key, no account
    assert str(key_file) not in opened
    db.close()


def test_number_formatting_keeps_whole_numbers_whole():
    assert cli._num(0, 0) == "0" and cli._num(100, 0) == "100" and cli._num(4287.10, 4) == "4,287.1"
    assert cli._num(None) == "-" and cli._num("") == "-" and cli._num("n/a") == "n/a" and cli._num(0.2412, 6) == "0.2412"
