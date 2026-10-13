"""Local read API, metrics endpoint and status dashboard (FR-094,
NFR-012, SR-09).

Bound to ``127.0.0.1`` by default. Everything except the liveness
check, the sign-in page and the logo needs a credential: a signed-in
dashboard account (roles ``viewer`` < ``operator`` < ``admin``, see
``hlbot.api.auth``) or the per-process token written to
``data/control.token`` (owner-only), which ``botctl`` uses and which
counts as ``admin``. Every command is validated by the backend: no risk
rule lives in a client.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from aiohttp import web

from hlbot import __version__
from hlbot.api import analytics, export, readmodel
from hlbot.api.auth import ROLES, Auth, AuthError, Principal
from hlbot.api.market import VolumeService
from hlbot.api.outlook import OutlookApi
from hlbot.config import short_address

if TYPE_CHECKING:  # pragma: no cover
    from hlbot.core.app import BotApp

logger = logging.getLogger("hlbot.api")

CONTROL_COMMANDS = frozenset({"pause", "resume", "ack", "kill", "close", "reconcile", "modify_sl", "inject"})
LOGO_MAX_BYTES = 512 * 1024
LOGO_FILE = "ui-logo"  # in the data directory; the type is read from the bytes, never from a file name
SESSION_COOKIE = "hlbot_session"
DEFAULT_NAME = "hlbot"
NAME_MAX = 40
NAME_KEY = "ui.name"
BROWSER_HEADER = "X-Requested-With"  # a cross-site page cannot set it without a CORS preflight, which is never granted
PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'"
)
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
PRINCIPAL = web.RequestKey("principal", Principal)  # who the role check let through


def sniff_image(data: bytes) -> str | None:
    """Content type of a raster image, decided from its first bytes.
    Anything else (SVG and HTML included, which can carry scripts) is
    refused, whatever the upload claims to be."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class ApiServer:
    def __init__(self, app: BotApp):
        self.bot = app
        self._runner: Any = None
        self._auth: Optional[Auth] = None
        self._volume = VolumeService(app)
        self._outlook = OutlookApi(app, lambda request: request[PRINCIPAL].name)
        self._public_hosts = frozenset(urlsplit(origin).netloc for origin in app.s.api_public_origins)

    @property
    def auth(self) -> Auth:
        if self._auth is None:
            self._auth = Auth(self.bot.db, self.bot.clock)
        return self._auth

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        application = web.Application(middlewares=[self._guard])
        viewer, operator, admin = (lambda handler, role=role: self._need(role, handler) for role in ROLES)
        application.add_routes(
            [
                # open: liveness, what the sign-in page needs, and signing in
                web.get("/health", self.health),
                web.get("/login", self.login_page),
                web.get("/logo", self.logo),
                web.get("/api/public", self.public),
                web.post("/api/login", self.login),
                web.post("/api/setup", self.setup),
                web.post("/api/logout", self.logout),
                web.get("/", self.dashboard),  # sends a signed-out browser to /login
                # viewer: read
                web.get("/api/session", viewer(self.whoami)),
                web.post("/api/account/password", viewer(self.change_password)),
                web.get("/metrics", viewer(self.metrics)),
                web.get("/api/status", viewer(self.status)),
                web.get("/api/price", viewer(self.price)),
                web.get("/api/equity", viewer(self.equity)),
                web.get("/api/trace", viewer(self.trace)),
                web.get("/api/market/volume", viewer(self.market_volume)),
                web.get("/api/analytics", viewer(self.analytics)),
                web.get("/api/trades", viewer(self.trades)),
                web.get("/api/export/trades.xlsx", viewer(self.export_trades)),
                # operator: commands
                web.post("/api/control/{name}", operator(self.control)),
                # admin: appearance and accounts
                web.post("/api/settings", admin(self.set_settings)),
                web.post("/api/settings/logo", admin(self.set_logo)),
                web.delete("/api/settings/logo", admin(self.reset_logo)),
                web.get("/api/users", admin(self.users)),
                web.post("/api/users", admin(self.create_user)),
                web.post("/api/users/{name}", admin(self.update_user)),
                web.delete("/api/users/{name}", admin(self.delete_user)),
            ]
        )
        application.add_routes(self._outlook.routes(viewer, operator))
        self._runner = web.AppRunner(application, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bot.s.api_host, self.bot.s.api_port)
        await site.start()
        logger.info("local API listening on http://%s:%s", self.bot.s.api_host, self.bot.s.api_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """When bound to loopback, refuse requests addressed to any other
        host name (DNS rebinding), and never emit CORS headers."""
        if self.bot.s.api_host in _LOOPBACK_HOSTS:
            host = request.host or ""
            if _hostname(host) not in _LOOPBACK_HOSTS and host.lower() not in self._public_hosts:
                return web.json_response({"error": "forbidden host"}, status=403)
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except AuthError as exc:
            response = web.json_response({"ok": False, "error": str(exc)}, status=exc.status)
        except Exception:  # noqa: BLE001
            logger.exception("API handler failed: %s", request.path)
            return web.json_response({"error": "internal error"}, status=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ who is asking
    def _principal(self, request: web.Request) -> Optional[Principal]:
        """The control token (``botctl`` on the bot host) or a signed-in
        browser session. A request that presents a wrong token is refused
        even if it also carries a valid cookie."""
        token = request.headers.get("X-Control-Token", "")
        if not token:
            scheme, _, value = request.headers.get("Authorization", "").partition(" ")
            token = value.strip() if scheme.lower() == "bearer" else ""
        if token:
            known = self.bot.control_token
            return Principal("local", "admin", "token") if known and hmac.compare_digest(token.encode(), known.encode()) else None
        return self.auth.session(request.cookies.get(SESSION_COOKIE, ""))

    def _public_origin(self, request: web.Request) -> Optional[str]:
        """The request's Origin, when it is one of the addresses this bot
        is published at through a reverse proxy or tunnel (API_PUBLIC_ORIGINS)."""
        origin = (request.headers.get("Origin") or "").rstrip("/").lower()
        return origin if origin in self.bot.s.api_public_origins else None

    def _page_refusal(self, request: web.Request) -> Optional[str]:
        """Cross-site request forgery check for writes from a browser.
        None when the request came from our own page: it carries our
        header (which another origin cannot send), and its Origin, when
        the browser states one, is this server or an address it is
        published at. The cookie is SameSite=Strict as well. Otherwise
        the reason, which names the setting when a proxy is in front."""
        if request.headers.get(BROWSER_HEADER) != "hlbot":
            return "request did not come from the dashboard"
        origin = request.headers.get("Origin")
        if origin is None or urlsplit(origin).netloc == request.host or self._public_origin(request):
            return None
        shown = "".join(c for c in origin[:120] if c.isprintable() and not c.isspace())
        return f"{shown} is not an address this bot is published at. On the bot host, set API_PUBLIC_ORIGINS={shown} in .env and restart"

    def _client(self, request: web.Request) -> str:
        """The caller's address for sign-in throttling. Behind a declared
        proxy every request arrives from the proxy, so its forwarding
        header is used instead."""
        if self.bot.s.api_public_origins:
            forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
            if forwarded.strip():
                return forwarded.strip()[:64]
        return request.remote or ""

    def _need(self, role: str, handler: Handler) -> Handler:
        async def guarded(request: web.Request) -> web.StreamResponse:
            principal = self._principal(request)
            if principal is None:
                return web.json_response({"ok": False, "error": "sign in required"}, status=401)
            if not principal.allows(role):
                return web.json_response({"ok": False, "error": f"your role ({principal.role}) cannot do this; it needs {role}"}, status=403)
            if principal.via == "session" and request.method not in ("GET", "HEAD"):
                refusal = self._page_refusal(request)
                if refusal:
                    return web.json_response({"ok": False, "error": refusal}, status=403)
            request[PRINCIPAL] = principal
            return await handler(request)

        return guarded

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json() if request.can_read_body else {}
        except ValueError as exc:
            raise AuthError("body must be JSON") from exc
        if not isinstance(body, dict):
            raise AuthError("body must be a JSON object")
        return body

    def _signed_in(self, request: web.Request, token: str, principal: Principal) -> web.Response:
        response = web.json_response({"ok": True, "user": principal.name, "role": principal.role})
        # HTTPS ends at the proxy, so the bot itself sees plain HTTP: go by the published address
        secure = request.secure or (self._public_origin(request) or "").startswith("https://")
        response.set_cookie(
            SESSION_COOKIE, token, max_age=self.auth.session_ms // 1000, httponly=True, samesite="Strict", secure=secure, path="/"
        )
        return response

    # ------------------------------------------------------------ sign-in
    def _name(self) -> str:
        return str(self.bot.db.kv_get(NAME_KEY) or DEFAULT_NAME)

    async def public(self, request: web.Request) -> web.Response:
        """The little the sign-in page needs before anyone has signed in."""
        return web.json_response({"name": self._name(), "setup_required": self.auth.user_count() == 0})

    async def login_page(self, request: web.Request) -> web.Response:
        if self._principal(request) is not None:
            return web.Response(status=302, headers={"Location": "/"})
        return self._page("login.html")

    async def login(self, request: web.Request) -> web.Response:
        refusal = self._page_refusal(request)
        if refusal:
            return web.json_response({"ok": False, "error": refusal}, status=403)
        body = await self._body(request)
        token, principal = await self.auth.login(body.get("username"), body.get("password"), self._client(request))
        return self._signed_in(request, token, principal)

    async def setup(self, request: web.Request) -> web.Response:
        """Create the first admin. Needs the control token, so only someone
        with access to the bot host can claim a fresh installation."""
        refusal = self._page_refusal(request)
        if refusal:
            return web.json_response({"ok": False, "error": refusal}, status=403)
        body = await self._body(request)
        known, given = self.bot.control_token, str(body.get("token") or "").strip()
        if not known or not hmac.compare_digest(given.encode(), known.encode()):
            return web.json_response({"ok": False, "error": "wrong control token"}, status=401)
        token, principal = await self.auth.setup_first_admin(body.get("username"), body.get("password"))
        return self._signed_in(request, token, principal)

    async def logout(self, request: web.Request) -> web.Response:
        refusal = self._page_refusal(request)
        if refusal:
            return web.json_response({"ok": False, "error": refusal}, status=403)
        self.auth.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def whoami(self, request: web.Request) -> web.Response:
        principal = request[PRINCIPAL]
        return web.json_response({"user": principal.name, "role": principal.role, "via": principal.via, "name": self._name()})

    async def change_password(self, request: web.Request) -> web.Response:
        principal = request[PRINCIPAL]
        if principal.via != "session":
            raise AuthError("the control token has no password; use botctl user passwd NAME")
        body = await self._body(request)
        await self.auth.change_own_password(principal.name, body.get("current"), body.get("new"), request.cookies.get(SESSION_COOKIE, ""))
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ accounts (admin)
    async def users(self, request: web.Request) -> web.Response:
        return web.json_response({"users": self.auth.list_users(), "roles": list(ROLES)}, dumps=_dumps)

    async def create_user(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        created = await self.auth.create_user(body.get("username"), body.get("password"), body.get("role"), by=request[PRINCIPAL].name)
        return web.json_response({"ok": True, **created})

    async def update_user(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        name, by = request.match_info["name"], request[PRINCIPAL].name
        unknown = set(body) - {"role", "disabled", "password"}
        if unknown or not body:
            raise AuthError("give role, disabled and/or password")
        result: dict[str, Any] = {"ok": True}
        if "role" in body or "disabled" in body:
            result.update(self.auth.update_user(name, by, role=body.get("role"), disabled=body.get("disabled")))
        if "password" in body:
            await self.auth.set_password(name, body["password"], by)
        return web.json_response(result)

    async def delete_user(self, request: web.Request) -> web.Response:
        self.auth.delete_user(request.match_info["name"], by=request[PRINCIPAL].name)
        if self.bot.outlook is not None:
            self.bot.outlook.forget_user(request.match_info["name"].strip().lower())
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ read
    def _live(self) -> dict[str, Any]:
        bot = self.bot
        now_ms = bot.clock.now_ms()
        stream = bot.adapter.stream_health()
        quotes = {}
        for coin in bot.s.coins:
            quote = bot.engine.quotes.get(coin)
            context = bot.engine.contexts.get(coin)
            quotes[coin] = {
                "bid": quote.bid if quote else None,
                "ask": quote.ask if quote else None,
                "age_ms": now_ms - quote.received_ms if quote else None,
                "mark": context.mark if context else None,
                "oracle": context.oracle if context else None,
                "funding": context.funding if context else None,
                # rolling 24 hours, as the exchange reports it: exact base (coin) and quote (collateral) volume
                "day_volume_base": context.day_volume_base if context else None,
                "day_volume_quote": context.day_volume_quote if context else None,
                "prev_day_price": context.prev_day_price if context else None,
            }
        view = bot.engine.last_view or bot.reconciler.last_view
        account = None
        exchange_orders = []
        if view is not None:
            account = {
                "age_ms": now_ms - view.account.received_ms,
                "by_dex": {d or "default": vars(a) for d, a in view.account.accounts_once().items()},
                "unified": view.account.unified,
                "positions": [vars(p) for c, p in view.account.positions.items() if c in bot.s.coins],
            }
            exchange_orders = [vars(o) for o in view.open_orders if o.coin in bot.s.coins]
        return {
            "version": __version__,
            "env": bot.s.label,
            "mode_note": bot.s.mode_note,
            "account_address": short_address(bot.s.account_address),
            "entries_enabled": bot.engine.entries_enabled,
            "coins": list(bot.s.coins),
            "quotes": quotes,
            "stream": {
                "connected": stream.connected,
                "message_age_ms": now_ms - stream.last_message_ms if stream.last_message_ms else None,
                "reconnects": stream.reconnects,
            },
            "latency": {"rest_ms": stream.rest_ms, "rest_avg_ms": stream.rest_avg_ms, "ws_ping_ms": stream.ping_ms},
            "telegram": {
                "enabled": bot.s.telegram_enabled,
                "connected": bool(bot.telegram and bot.telegram.connected),
                "channels": [str(c) for c in (bot.telegram.resolved if bot.telegram else [])],
            },
            "exchange_account": account,
            "exchange_open_orders": exchange_orders,
            "reconciler": bot.reconciler.status(),
            "fee_rate": bot.engine.fee_rate,
            "metrics": bot.metrics.snapshot(),
        }

    async def health(self, request: web.Request) -> web.Response:
        state = self.bot.health.state()
        return web.json_response({"status": "ok", "health": state, "env": self.bot.s.label, "version": __version__})

    async def status(self, request: web.Request) -> web.Response:
        data = readmodel.build_status(self.bot.db, self.bot.clock)
        data["live"] = self._live()
        return web.json_response(data, dumps=_dumps)

    async def price(self, request: web.Request) -> web.Response:
        return web.json_response(self._live()["quotes"], dumps=_dumps)

    async def equity(self, request: web.Request) -> web.Response:
        """Account value over time for the dashboard chart: ``?range=1h|1d|1w``."""
        dexes = sorted({inst.dex for inst in self.bot.engine.instruments.values()})
        try:
            data = readmodel.equity_history(self.bot.db, self.bot.clock, request.query.get("range", "1d"), dexes)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(data, dumps=_dumps)

    async def trace(self, request: web.Request) -> web.Response:
        try:
            keys = {k: int(v) for k, v in request.query.items() if k in ("fill", "order", "trade", "signal")}
        except ValueError:
            return web.json_response({"error": "ids must be integers"}, status=400)
        if not keys:
            return web.json_response({"error": "give one of fill, order, trade, signal"}, status=400)
        return web.json_response(readmodel.trace(self.bot.db, **keys), dumps=_dumps)

    # ------------------------------------------------------------ market volume, analytics, history, export
    def _dexes(self) -> list[str]:
        return sorted({inst.dex for inst in self.bot.engine.instruments.values()})

    async def market_volume(self, request: web.Request) -> web.Response:
        """Volume per managed symbol for one candle interval: ``?interval=5m|15m|1h|4h|1d``."""
        try:
            return web.json_response(await self._volume.get(request.query.get("interval")), dumps=_dumps)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def analytics(self, request: web.Request) -> web.Response:
        """Performance figures and chart series for the filtered history:
        ``?from=&to=&tz=&symbol=&source=&side=&status=``."""
        try:
            chosen = analytics.parse_filter(request.query)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(analytics.analytics(self.bot.db, chosen, self.bot.clock, self._dexes()), dumps=_dumps)

    async def trades(self, request: web.Request) -> web.Response:
        """One page of the filtered trade history, newest first, each trade with its orders."""
        try:
            chosen = analytics.parse_filter(request.query)
            page = int(request.query.get("page", "1"))
            size = int(request.query.get("page_size", "20"))
            if size not in analytics.PAGE_SIZES:
                raise ValueError(f"page_size must be one of {', '.join(map(str, analytics.PAGE_SIZES))}")
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(analytics.trades_page(self.bot.db, chosen, page, size), dumps=_dumps)

    async def export_trades(self, request: web.Request) -> web.Response:
        """Every trade matching the filter as an Excel workbook. The same filter as the history, without paging."""
        try:
            chosen = analytics.parse_filter(request.query)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if analytics.count_trades(self.bot.db, chosen) > export.MAX_TRADES:
            return web.json_response({"error": f"more than {export.MAX_TRADES:,} trades match; narrow the date range"}, status=413)
        data = analytics.export_data(self.bot.db, chosen)
        now = self.bot.clock.now()
        # the database was read on the event loop; packing the file is plain CPU work and must not hold up trading
        body = await asyncio.to_thread(export.build_workbook, data, env_label=self.bot.s.label, generated=now)
        name = export.filename(data, now)
        self.bot.db.audit("export.trades", {"by": request[PRINCIPAL].name, "filter": chosen.describe(), "trades": len(data["trades"]), "bytes": len(body)})
        response = web.Response(body=body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["X-Export-Rows"] = str(len(data["trades"]))
        return response

    async def metrics(self, request: web.Request) -> web.Response:
        return web.Response(text=self.bot.metrics.render(), content_type="text/plain")

    @staticmethod
    def _page(name: str) -> web.Response:
        html = resources.files("hlbot.api").joinpath(name).read_text(encoding="utf-8")
        response = web.Response(text=html, content_type="text/html")
        response.headers["Content-Security-Policy"] = PAGE_CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def dashboard(self, request: web.Request) -> web.Response:
        if self._principal(request) is None:
            return web.Response(status=302, headers={"Location": "/login"})
        return self._page("dashboard.html")

    # ------------------------------------------------------------ appearance: the name
    async def set_settings(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        if set(body) != {"name"} or not isinstance(body["name"], str):
            raise AuthError('give {"name": "..."}')
        name = " ".join(body["name"].split())
        if len(name) > NAME_MAX or re.search(r"[\x00-\x1f\x7f]", name):
            raise AuthError(f"name must be at most {NAME_MAX} characters, without control characters")
        self.bot.db.kv_set(NAME_KEY, name or None)  # empty: back to the default
        self.bot.db.audit("settings.name", {"name": name or DEFAULT_NAME, "by": request[PRINCIPAL].name})
        return web.json_response({"ok": True, "name": self._name()})

    # ------------------------------------------------------------ appearance: the logo
    def _logo_path(self) -> Any:
        return self.bot.s.data_path / LOGO_FILE

    async def logo(self, request: web.Request) -> web.Response:
        """The custom logo when one has been uploaded, otherwise the default."""
        path = self._logo_path()
        if path.exists():
            data = path.read_bytes()
            kind = sniff_image(data)
            if kind:
                return web.Response(body=data, content_type=kind)
        data = resources.files("hlbot.api").joinpath("logo.png").read_bytes()
        return web.Response(body=data, content_type="image/png")

    async def set_logo(self, request: web.Request) -> web.Response:
        if (request.content_length or 0) > LOGO_MAX_BYTES:
            return web.json_response({"ok": False, "error": f"logo must be at most {LOGO_MAX_BYTES // 1024} KB"}, status=413)
        data = b""
        while len(data) <= LOGO_MAX_BYTES:  # a stream read may return less than was asked for
            chunk = await request.content.read(LOGO_MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > LOGO_MAX_BYTES:
            return web.json_response({"ok": False, "error": f"logo must be at most {LOGO_MAX_BYTES // 1024} KB"}, status=413)
        kind = sniff_image(data)
        if kind is None:
            return web.json_response({"ok": False, "error": "logo must be a PNG, JPEG, WebP or GIF image"}, status=400)
        path = self._logo_path()
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(data)
        temp.chmod(0o600)
        temp.replace(path)
        self.bot.db.audit("settings.logo", {"action": "set", "type": kind, "bytes": len(data), "by": request[PRINCIPAL].name})
        return web.json_response({"ok": True, "type": kind, "bytes": len(data)})

    async def reset_logo(self, request: web.Request) -> web.Response:
        path = self._logo_path()
        existed = path.exists()
        if existed:
            path.unlink()
            self.bot.db.audit("settings.logo", {"action": "reset", "by": request[PRINCIPAL].name})
        return web.json_response({"ok": True, "was_custom": existed})

    # ------------------------------------------------------------ control
    async def control(self, request: web.Request) -> web.Response:
        principal = request[PRINCIPAL]
        name = request.match_info["name"]
        if name not in CONTROL_COMMANDS:
            return web.json_response({"ok": False, "error": f"unknown command {name!r}"}, status=404)
        try:
            args = await request.json() if request.can_read_body else {}
        except ValueError:
            return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)
        if not isinstance(args, dict):
            return web.json_response({"ok": False, "error": "body must be a JSON object"}, status=400)
        if name == "kill" and args.get("confirm") != "KILL":
            return web.json_response({"ok": False, "error": 'kill requires {"confirm": "KILL"}'}, status=400)
        by = str(args.pop("by", "api"))[:40]
        if principal.via == "session":
            by = principal.name  # a browser does not get to choose the name written to the audit trail
        try:
            result = await self.bot.command(name, args, by=by)
        except (KeyError, ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": f"bad arguments: {exc}"}, status=400)
        return web.json_response(result, dumps=_dumps)


def _hostname(host: str) -> str:
    """Host header without the port: ``127.0.0.1:8787`` -> ``127.0.0.1``,
    ``[::1]:8787`` -> ``[::1]``."""
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
