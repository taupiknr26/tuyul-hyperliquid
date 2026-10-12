"""``botctl``: operator control tool (FR-093).

Every verb of the baseline ``bot.bat`` / ``bot.sh`` has an equivalent
here, cross-platform and without a process-name scan:

    start stop restart status logs orders price positions pending
    config test                               (baseline verbs)
    pause resume kill close reconcile ack     (kill switch and control)
    run trace inject modify-sl backup telegram-login init-secrets

Read-only verbs fall back to the database when the bot is not running.
``pause`` and ``resume`` also work offline, because the pause state is
persisted and read at the next start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from hlbot import __version__
from hlbot.config import ConfigError, Settings, load_settings

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_RUNNING = 0, 1, 2, 3


# ---------------------------------------------------------------------- helpers


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _settings(args: argparse.Namespace, *, mainnet_flag: Optional[bool] = None) -> Settings:
    """``run``, ``start`` and ``config`` pass their ``--mainnet`` flag.
    Every other verb looks at what a bot left behind and sends no order
    itself, so with HL_ENV=mainnet it follows the mode that bot last
    started in (the instance lock is rewritten on every start) instead of
    the paper fallback of a command line without the flag: otherwise
    ``orders``, ``trace`` and ``backup`` would read the paper database
    while the bot trades on mainnet."""
    env_file = getattr(args, "env_file", ".env")
    if mainnet_flag is not None:
        return load_settings(env_file=env_file, mainnet_flag=mainnet_flag)
    settings = load_settings(env_file=env_file)
    if settings.requested_env == "mainnet" and settings.env != "mainnet":
        try:
            flagged = load_settings(env_file=env_file, mainnet_flag=True)
        except ConfigError:
            return settings
        if flagged.env == "mainnet" and _mtime(flagged.lock_path) > _mtime(settings.lock_path):
            return flagged
    return settings


def _pid(settings: Settings) -> Optional[int]:
    try:
        pid = int(settings.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _api(settings: Settings, path: str, *, method: str = "GET", body: Optional[dict[str, Any]] = None, timeout: float = 5.0) -> Any:
    """Call the local API. Raises ConnectionError when the bot is not reachable."""
    host = "127.0.0.1" if settings.api_host in ("0.0.0.0", "::") else settings.api_host
    url = f"http://{host}:{settings.api_port}{path}"
    headers = {"Content-Type": "application/json"}
    try:  # every call carries the local token: the API serves nothing but /health without a credential
        headers["X-Control-Token"] = settings.control_token_path.read_text().strip()
    except OSError as exc:
        if method != "GET":
            raise ConnectionError("the bot is not running (no control token)") from exc
    data = json.dumps(body or {}).encode() if method == "POST" else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except ValueError:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectionError("the bot is not reachable on its local API") from exc


def _offline_db(settings: Settings, readonly: bool = True):
    from hlbot.db.database import Database, DatabaseError

    try:
        return Database(settings.db_path, readonly=readonly)
    except DatabaseError:
        return None


def _status(settings: Settings) -> tuple:
    """(status dict, live?)"""
    try:
        return _api(settings, "/api/status"), True
    except ConnectionError:
        db = _offline_db(settings)
        if db is None:
            return None, False
        from hlbot.api import readmodel

        try:
            return readmodel.build_status(db), False
        finally:
            db.close()


def _num(value: Any, digits: int = 4) -> str:
    if value is None or value == "":
        return "-"
    try:
        text = f"{float(value):,.{digits}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text  # never strip the zeros of a whole number
    except (TypeError, ValueError):
        return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(h) for h in headers]] + [["-" if c is None else str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


# ---------------------------------------------------------------------- run / start / stop


def cmd_run(args: argparse.Namespace) -> int:
    """Run in the foreground (what the supervisor executes)."""
    from hlbot.core.app import AlreadyRunning, BotApp
    from hlbot.core.reconciler import StartupError
    from hlbot.db.database import DatabaseCorrupt
    from hlbot.ingest.telegram import ChannelResolutionError
    from hlbot.logging_setup import setup_logging

    try:
        settings = _settings(args, mainnet_flag=args.mainnet)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(settings.log_path, settings.log_level, console=not args.quiet)
    banner = f"hlbot {__version__} — mode {settings.label}"
    print(banner, file=sys.stderr)
    if settings.mode_note:
        print(f"NOTE: {settings.mode_note}", file=sys.stderr)
    if settings.env == "mainnet":
        print("MAINNET: real funds. Orders will be sent to the configured account.", file=sys.stderr)

    async def main() -> int:
        app = BotApp(settings)
        try:
            return await app.run()
        except AlreadyRunning as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return EXIT_RUNNING
        except (ConfigError, StartupError, ChannelResolutionError, DatabaseCorrupt) as exc:
            print(f"startup failed: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    return asyncio.run(main())


def cmd_start(args: argparse.Namespace) -> int:
    try:
        settings = _settings(args, mainnet_flag=args.mainnet)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if _pid(settings):
        print("Already running — run 'botctl stop' first (two instances would double-send orders).")
        return EXIT_RUNNING
    print(f"Starting in {settings.label} mode" + (" — REAL FUNDS" if settings.env == "mainnet" else "") + ".")
    if settings.mode_note:
        print(f"NOTE: {settings.mode_note}")
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "hlbot", "--env-file", args.env_file, "run", "--quiet"] + (["--mainnet"] if args.mainnet else [])
    out = open(Path(settings.log_dir) / "bot.out", "ab")  # noqa: SIM115 - handed to the child
    process = subprocess.Popen(command, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            print(f"The bot exited during startup (code {process.returncode}). See {settings.log_dir}/bot.out")
            return EXIT_ERROR
        if _pid(settings):
            break
        time.sleep(0.5)
    else:
        print("Still starting after 60 s (recovery may be in progress). Check: botctl status")
        return EXIT_OK
    for _ in range(16):  # give the market stream a moment to connect so the first status is meaningful
        try:
            if _api(settings, "/api/status", timeout=2.0)["live"]["stream"]["connected"]:
                break
        except (ConnectionError, KeyError, TypeError):
            pass
        time.sleep(0.5)
    return cmd_status(args)


def cmd_stop(args: argparse.Namespace) -> int:
    settings = _settings(args)
    pid = _pid(settings)
    if not pid:
        print("Not running.")
        return EXIT_OK
    # Ask nicely first: the stop flag lets the bot send its "stopped"
    # notification and disconnect cleanly before it exits.
    settings.data_path.mkdir(parents=True, exist_ok=True)
    settings.stop_flag_path.write_text("stop")
    print("Asked the bot to stop gracefully; waiting up to 20 s...")
    for _ in range(40):
        if not _pid(settings):
            print("Stopped. Exchange-resident SL/TP orders remain in place for any open position.")
            return EXIT_OK
        time.sleep(0.5)
    print("Graceful stop timed out; sending SIGTERM...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(20):
        if not _pid(settings):
            print("Stopped.")
            return EXIT_OK
        time.sleep(0.5)
    print(f"Process {pid} is still running. Investigate before forcing it: kill -9 {pid}")
    return EXIT_ERROR


def cmd_restart(args: argparse.Namespace) -> int:
    code = cmd_stop(args)
    return code if code != EXIT_OK else cmd_start(args)


# ---------------------------------------------------------------------- read-only verbs


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    status, live = _status(settings)
    pid = _pid(settings)
    print(f"Process : {'running (pid ' + str(pid) + ')' if pid else 'NOT running'}")
    if status is None:
        print("No database yet — the bot has never been started with this configuration.")
        return EXIT_OK
    if getattr(args, "json", False):
        _print_json(status)
        return EXIT_OK
    health = status["health"]
    info = status.get("live", {})
    print(f"Mode    : {info.get('env', settings.label)}" + ("" if live else "  (offline view from the database)"))
    if info.get("mode_note") or settings.mode_note:
        print(f"Note    : {info.get('mode_note') or settings.mode_note}")
    print(f"Health  : {health['state']}   control={health['control']['mode']}" + (f" ({health['control']['reason']})" if health["control"]["reason"] else ""))
    for condition in health["conditions"]:
        print(f"  - [{condition['level']}] {condition['key']}: {condition['reason']} ({'auto' if condition['auto_resume'] else 'manual resume'})")
    if live:
        stream = info["stream"]
        print(f"Stream  : {'connected' if stream['connected'] else 'DOWN'}, last message {_num(stream['message_age_ms'], 0)} ms ago, {stream['reconnects']} reconnect(s)")
        latency = info.get("latency") or {}
        if latency.get("rest_ms") is not None or latency.get("ws_ping_ms") is not None:
            print(f"Latency : API {_num(latency.get('rest_ms'), 0)} ms (avg {_num(latency.get('rest_avg_ms'), 0)} ms), stream ping {_num(latency.get('ws_ping_ms'), 0)} ms")
        telegram = info["telegram"]
        print(f"Telegram: {'disabled' if not telegram['enabled'] else ('connected' if telegram['connected'] else 'DISCONNECTED')}")
        print(f"Reconcile: last ok {info['reconciler']['last_ok_at'] or '-'}")
    print(f"Open trades: {len(status['open_trades'])}   waiting signals: {len(status['waiting_signals'])}   outbox pending: {status['outbox_pending']}")
    day = status["day"]
    print(f"Day ({day['utc_date']} UTC): realised {_num(day.get('realized'), 2)}  fees {_num(day.get('fees'), 2)}  funding {_num(day.get('funding'), 2)}  start equity {_num(day.get('start_equity'), 2)}")
    return EXIT_OK


def cmd_logs(args: argparse.Namespace) -> int:
    settings = _settings(args)
    path = settings.log_path
    if not path.exists():
        print(f"No log file yet at {path}")
        return EXIT_OK

    def show(line: str) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            print(line.rstrip())
            return
        extra = {k: v for k, v in record.items() if k not in ("ts", "level", "logger", "msg", "corr")}
        tail = (" " + json.dumps(extra, default=str)) if extra else ""
        print(f"{record.get('ts', '')} {record.get('level', ''):7} {record.get('logger', '')}: {record.get('msg', '')}{tail}")

    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle.readlines()[-args.lines :]:
            show(line)
        if not args.follow:
            return EXIT_OK
        try:
            while True:
                line = handle.readline()
                if line:
                    show(line)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return EXIT_OK


def cmd_orders(args: argparse.Namespace) -> int:
    settings = _settings(args)
    db = _offline_db(settings)
    if db is None:
        print("No database yet.")
        return EXIT_OK
    from hlbot.api import readmodel

    rows = readmodel.recent_orders(db, args.count)
    db.close()
    print(_table(
        ["id", "trade", "role", "leg", "side", "type", "size", "price", "trigger", "filled", "avg", "state", "exchange", "updated"],
        [[r["id"], r["trade_id"], r["role"], r["leg"], r["side"], r["type"] + ("/" + r["tif"] if r["tif"] else ""), _num(r["size"], 6),
          _num(r["price"]), _num(r["trigger_price"]), _num(r["filled_size"], 6), _num(r["avg_fill_price"]), r["state"],
          r["exchange_status"], r["updated_at"]] for r in rows],
    ) if rows else "No orders recorded.")
    return EXIT_OK


def cmd_price(args: argparse.Namespace) -> int:
    settings = _settings(args)
    try:
        quotes = _api(settings, "/api/price")
    except ConnectionError:
        # Bot not running: one-off read of the public book (no account, no key).
        from hlbot.exchange.hyperliquid import HyperliquidAdapter

        async def fetch() -> dict[str, Any]:
            adapter = HyperliquidAdapter(rest_url=settings.rest_url, ws_url=settings.ws_url, is_mainnet=settings.market_data_env == "mainnet", coins=settings.coins)
            result: dict[str, Any] = {}
            try:
                for coin in settings.coins:
                    quote = await adapter.get_book(coin)
                    context = await adapter.get_market(coin)
                    result[coin] = {"bid": quote.bid, "ask": quote.ask, "mark": context.mark, "oracle": context.oracle, "age_ms": 0}
            finally:
                await adapter.stop()
            return result

        try:
            quotes = asyncio.run(fetch())
        except Exception as exc:  # noqa: BLE001
            print(f"price unavailable: {exc}", file=sys.stderr)
            return EXIT_ERROR
    print(_table(["coin", "bid", "ask", "mark", "oracle", "age ms"],
                 [[c, _num(q["bid"]), _num(q["ask"]), _num(q["mark"]), _num(q["oracle"]), _num(q["age_ms"], 0)] for c, q in quotes.items()]))
    return EXIT_OK


def cmd_positions(args: argparse.Namespace) -> int:
    settings = _settings(args)
    status, live = _status(settings)
    if status is None:
        print("No database yet.")
        return EXIT_OK
    account = (status.get("live") or {}).get("exchange_account") if live else None
    if account:
        rows = [[p["coin"], _num(p["szi"], 6), _num(p["entry_px"]), _num(p["unrealized_pnl"], 2), _num(p["leverage"], 0), _num(p["liquidation_px"], 2),
                 _num(p["margin_used"], 2), _num(p["cum_funding"], 4)] for p in account["positions"]]
        print(f"Exchange snapshot, {_num(account['age_ms'] / 1000, 1)} s old:")
    else:
        rows = [[p["coin"], _num(p["szi"], 6), _num(p["entry_px"]), _num(p["unrealized_pnl"], 2), _num(p["leverage"], 0), _num(p["liquidation_px"], 2),
                 _num(p["margin_used"], 2), _num(p["cum_funding"], 4)] for p in status["positions"] if p["szi"]]
        ages = [p["age_s"] for p in status["positions"] if p.get("age_s") is not None]
        print(f"Last stored snapshot ({'bot not running' if not live else 'no live view yet'}){', ' + _num(max(ages), 0) + ' s old' if ages else ''}:")
    print(_table(["coin", "size", "entry", "uPnL", "lev", "liq px", "margin", "funding"], rows) if rows else "Flat on every managed asset.")
    trades = status["open_trades"]
    if trades:
        print("\nBot trades:")
        print(_table(["trade", "coin", "side", "state", "filled", "remaining", "entry", "SL"],
                     [[t["id"], t["coin"], t["side"], t["state"], _num(t["filled_size"], 6), _num(t["remaining_size"], 6), _num(t["avg_entry"]), _num(t["sl_price"])] for t in trades]))
    return EXIT_OK


def cmd_pending(args: argparse.Namespace) -> int:
    settings = _settings(args)
    status, _ = _status(settings)
    if status is None:
        print("No database yet.")
        return EXIT_OK
    rows = status["waiting_signals"]
    print(_table(["signal", "channel", "content", "coin", "range low", "range high", "SL", "attempts", "expires", "note"],
                 [[s["id"], s["channel_ref"], s["content_id"], s["coin"], _num(s["effective_low"]), _num(s["effective_high"]), _num(s["sl"]),
                   s["attempts"], s["expires_at"], s["reason"] or ""] for s in rows]) if rows else "No signals waiting for price.")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Resolved settings with every secret hidden."""
    try:
        settings = _settings(args, mainnet_flag=getattr(args, "mainnet", False))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    data = settings.public_dict()
    risk = data.pop("risk")
    print(f"mode: {settings.label}" + (f"   ({settings.mode_note})" if settings.mode_note else ""))
    for key in sorted(data):
        print(f"{key} = {data[key]}")
    print("\n[risk]")
    for key in sorted(risk):
        print(f"{key} = {risk[key]}")
    for warning in settings.warnings:
        print(f"WARNING: {warning}")
    return EXIT_OK


def cmd_trace(args: argparse.Namespace) -> int:
    settings = _settings(args)
    keys = {k: getattr(args, k) for k in ("fill", "order", "trade", "signal") if getattr(args, k) is not None}
    if not keys:
        print("give one of --fill, --order, --trade, --signal", file=sys.stderr)
        return EXIT_ERROR
    db = _offline_db(settings)
    if db is None:
        print("No database yet.")
        return EXIT_OK
    from hlbot.api import readmodel

    result = readmodel.trace(db, **keys)
    db.close()
    _print_json(result)
    return EXIT_OK if result.get("found") else EXIT_ERROR


# ---------------------------------------------------------------------- control verbs


def _control(args: argparse.Namespace, name: str, body: Optional[dict[str, Any]] = None, timeout: float = 10.0) -> int:
    settings = _settings(args)
    try:
        result = _api(settings, f"/api/control/{name}", method="POST", body={**(body or {}), "by": "botctl"}, timeout=timeout)
    except ConnectionError as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _print_json(result)
    return EXIT_OK if result.get("ok") else EXIT_ERROR


def _offline_control(args: argparse.Namespace, mode: str) -> int:
    """pause / resume while the bot is down: the state is read at the next start."""
    from hlbot.core.health import Health
    from hlbot.db.database import Database

    settings = _settings(args)
    db = Database(settings.db_path)
    db.migrate()
    health = Health(db)
    if mode == "pause":
        health.pause(getattr(args, "reason", None) or "operator", "botctl (offline)")
        print("Paused (stored). The bot will start paused.")
    else:
        cleared = health.resume("botctl (offline)")
        print("Resumed (stored)." + (f" Cleared: {', '.join(cleared)}" if cleared else ""))
    db.close()
    return EXIT_OK


def cmd_pause(args: argparse.Namespace) -> int:
    if _pid(_settings(args)):
        return _control(args, "pause", {"reason": args.reason})
    return _offline_control(args, "pause")


def cmd_resume(args: argparse.Namespace) -> int:
    if _pid(_settings(args)):
        return _control(args, "resume")
    return _offline_control(args, "resume")


def cmd_kill(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("KILL closes every bot position at market (reduce-only), cancels bot orders and pauses. Type KILL to confirm: ")
        if answer.strip() != "KILL":
            print("Aborted.")
            return EXIT_ERROR
    return _control(args, "kill", {"confirm": "KILL"}, timeout=45.0)


def cmd_close(args: argparse.Namespace) -> int:
    return _control(args, "close", {"trade_id": args.trade})


def cmd_reconcile(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.adopt is not None:
        body = {"adopt": True, "trade_id": args.adopt}
    elif args.close is not None:
        body = {"close": True, "trade_id": args.close}
    return _control(args, "reconcile", body, timeout=30.0)


def cmd_ack(args: argparse.Namespace) -> int:
    return _control(args, "ack")


def cmd_modify_sl(args: argparse.Namespace) -> int:
    return _control(args, "modify_sl", {"trade_id": args.trade, "price": args.price})


def cmd_inject(args: argparse.Namespace) -> int:
    text = args.text if args.text != "-" else sys.stdin.read()
    return _control(args, "inject", {"text": text.replace("\\n", "\n"), "channel": args.channel})


# ---------------------------------------------------------------------- maintenance


def _read_password(args: argparse.Namespace, who: str) -> Optional[str]:
    """A new password, typed twice without echo; or one line from stdin
    with --password-stdin. Never taken from the command line, where it
    would land in the shell history and the process list."""
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    import getpass

    first = getpass.getpass(f"New password for {who}: ")
    if first != getpass.getpass("Repeat it: "):
        print("The two entries differ; nothing was changed.", file=sys.stderr)
        return None
    return first


def cmd_user(args: argparse.Namespace) -> int:
    """Dashboard accounts, through the running bot (local control token)."""
    settings = _settings(args)
    action, name = str(args.action), str(getattr(args, "name", "") or "")
    path = "/api/users" + (f"/{urllib.parse.quote(name, safe='')}" if name and action != "add" else "")
    try:
        if action == "list":
            users = _api(settings, "/api/users").get("users")
            if users is None:
                print("FAILED: could not read the accounts", file=sys.stderr)
                return EXIT_ERROR
            if not users:
                print("No accounts yet. Create the first one: botctl user add NAME --role admin")
            for user in users:
                state = "disabled" if user["disabled"] else "active"
                print(f"{user['username']:<24} {user['role']:<9} {state:<9} last sign-in: {user['last_login_at'] or 'never'}")
            return EXIT_OK
        if action == "add":
            password = _read_password(args, name)
            if password is None:
                return EXIT_ERROR
            result = _api(settings, path, method="POST", body={"username": name, "password": password, "role": args.role})
        elif action == "passwd":
            password = _read_password(args, name)
            if password is None:
                return EXIT_ERROR
            result = _api(settings, path, method="POST", body={"password": password})
        elif action == "role":
            result = _api(settings, path, method="POST", body={"role": args.role})
        elif action in ("disable", "enable"):
            result = _api(settings, path, method="POST", body={"disabled": action == "disable"})
        else:  # remove
            result = _api(settings, path, method="DELETE")
    except ConnectionError as exc:
        print(f"{exc}. Accounts are managed through the running bot.", file=sys.stderr)
        return EXIT_ERROR
    if not result.get("ok"):
        print(f"FAILED: {result.get('error', 'unknown error')}", file=sys.stderr)
        return EXIT_ERROR
    print({"add": "created", "passwd": "password changed; that account is signed out everywhere", "role": "role changed",
           "disable": "disabled and signed out", "enable": "enabled", "remove": "removed"}[action] + f": {name}")
    return EXIT_OK


def cmd_test(args: argparse.Namespace) -> int:
    return subprocess.call([sys.executable, "-m", "pytest", *args.pytest_args])


def cmd_backup(args: argparse.Namespace) -> int:
    from hlbot.db.database import Database

    settings = _settings(args)
    if not settings.db_path.exists():
        print("No database to back up.")
        return EXIT_ERROR
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = Path(args.destination) if args.destination else settings.data_path / "backups" / f"{settings.db_path.stem}-{stamp}.sqlite3"
    db = Database(settings.db_path, readonly=True)
    try:
        print(f"Backup written to {db.backup(destination)}")
    finally:
        db.close()
    return EXIT_OK


def cmd_init_secrets(args: argparse.Namespace) -> int:
    """Create an owner-only secrets file template outside the repository."""
    path = Path(os.path.expanduser(args.path))
    if path.exists():
        print(f"{path} already exists; not overwriting.")
        return EXIT_ERROR
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("# hlbot secrets. Owner-only (chmod 600). Never commit this file.\nTELEGRAM_API_HASH=\nTELEGRAM_BOT_TOKEN=\n")
    print(f"Created {path} (mode 0600). Fill it in, then set SECRETS_FILE={path} in .env")
    print("The Hyperliquid agent key goes in its OWN owner-only file, named by HL_AGENT_KEY_FILE.")
    return EXIT_OK


def cmd_telegram_login(args: argparse.Namespace) -> int:
    """Interactive first login. Creates the session file with mode 0600."""
    from hlbot.ingest.telegram import build_client

    try:
        settings = _settings(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    async def login() -> None:
        client = build_client(settings)
        await client.start()  # prompts for phone number and code on the terminal
        me = await client.get_me()
        print(f"Logged in as user id ending …{str(me.id)[-3:]}. Session stored at {settings.session_path}")
        await client.disconnect()

    old_mask = os.umask(0o077)
    try:
        asyncio.run(login())
    finally:
        os.umask(old_mask)
    if settings.session_path.exists():
        settings.session_path.chmod(0o600)
    return EXIT_OK


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="botctl", description="Control the Hyperliquid signal trading bot.")
    parser.add_argument("--env-file", default=".env", help="settings file (default: .env)")
    parser.add_argument("--version", action="version", version=f"hlbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Any, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(fn=fn)
        return p

    for name, fn, text in (("run", cmd_run, "run in the foreground (used by the supervisor)"),
                           ("start", cmd_start, "start in the background"),
                           ("restart", cmd_restart, "stop, then start")):
        p = add(name, fn, text)
        p.add_argument("--mainnet", action="store_true", help="explicit flag required for mainnet (also needs HL_MAINNET_CONFIRM)")
        if name == "run":
            p.add_argument("--quiet", action="store_true", help="log to file only")
    add("stop", cmd_stop, "graceful stop (exchange-resident orders stay)")
    p = add("status", cmd_status, "process, health, conditions, counts")
    p.add_argument("--json", action="store_true")
    p = add("logs", cmd_logs, "show the last N log lines")
    p.add_argument("lines", nargs="?", type=int, default=30)
    p.add_argument("-f", "--follow", action="store_true")
    p = add("orders", cmd_orders, "the last N bot orders")
    p.add_argument("count", nargs="?", type=int, default=20)
    add("price", cmd_price, "current bid/ask/mark of the managed coins")
    add("positions", cmd_positions, "open positions and bot trades")
    add("pending", cmd_pending, "signals waiting for price")
    p = add("config", cmd_config, "resolved settings (secrets hidden)")
    p.add_argument("--mainnet", action="store_true")
    p = add("pause", cmd_pause, "stop new entries; open trades keep being managed")
    p.add_argument("--reason", default="operator")
    add("resume", cmd_resume, "allow entries again; clears manual-resume conditions")
    p = add("kill", cmd_kill, "pause, close every bot position reduce-only, cancel bot orders")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p = add("close", cmd_close, "close one bot trade reduce-only")
    p.add_argument("trade", type=int)
    p = add("reconcile", cmd_reconcile, "run a reconciliation pass now")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--adopt", type=int, metavar="TRADE", help="accept the exchange position as this trade's size")
    group.add_argument("--close", type=int, metavar="TRADE", help="close this trade")
    add("ack", cmd_ack, "acknowledge a HALTED alert (stops the repeat)")
    p = add("modify-sl", cmd_modify_sl, "move the stop-loss of an open trade")
    p.add_argument("trade", type=int)
    p.add_argument("price", type=float)
    p = add("inject", cmd_inject, "feed a message as if a channel posted it (never on mainnet)")
    p.add_argument("text", help="message text, or - for stdin")
    p.add_argument("--channel", default="inject")
    p = add("trace", cmd_trace, "full chain of events for a fill, order, trade or signal")
    for key in ("fill", "order", "trade", "signal"):
        p.add_argument(f"--{key}", type=int)
    p = add("test", cmd_test, "run the test suite")
    p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    p = add("backup", cmd_backup, "write a consistent database backup")
    p.add_argument("destination", nargs="?")
    p = add("init-secrets", cmd_init_secrets, "create an owner-only secrets file template")
    p.add_argument("path")
    add("telegram-login", cmd_telegram_login, "interactive Telegram login (creates the session file)")
    p = add("user", cmd_user, "dashboard accounts: list, add, passwd, role, disable, enable, remove")
    actions = p.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="every account, its role and last sign-in")
    for action, text in (("add", "create an account (asks for the password)"), ("passwd", "set a new password"),
                         ("role", "change the role"), ("disable", "block sign-in and end its sessions"),
                         ("enable", "allow sign-in again"), ("remove", "delete the account")):
        q = actions.add_parser(action, help=text, description=text)
        q.add_argument("name")
        if action in ("add", "role"):
            q.add_argument("--role", choices=("viewer", "operator", "admin"), required=action == "role", default="viewer")
        if action in ("add", "passwd"):
            q.add_argument("--password-stdin", action="store_true", help="read the password from standard input instead of prompting")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
