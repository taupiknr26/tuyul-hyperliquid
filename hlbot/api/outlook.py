"""HTTP face of the scenario outlook (docs/outlook.md §9).

Reading needs ``viewer``. A user's own layer ("My Version": custom
levels, scenario edits, a journal note) belongs to that user and is
saved by the same role: it changes nothing but what that user sees.
Asking for a fresh brief reaches out to the sources and replaces the
running brief for everyone, so it needs ``operator`` and is rate-limited.

Everything a browser sends is validated in the service; no rule lives
in the page.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from hlbot.core.outlook import OutlookError

if TYPE_CHECKING:  # pragma: no cover
    from hlbot.core.app import BotApp

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Guard = Callable[[Handler], Handler]
OFF = "the scenario outlook is switched off (OUTLOOK_ENABLED, OUTLOOK_COIN)"


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


class OutlookApi:
    def __init__(self, bot: BotApp, user_of: Callable[[web.Request], str]):
        self.bot = bot
        self._user_of = user_of

    def routes(self, viewer: Guard, operator: Guard) -> list[web.RouteDef]:
        return [
            web.get("/api/outlook/latest", viewer(self._safe(self.latest))),
            web.get("/api/outlook/history", viewer(self._safe(self.history))),
            web.get("/api/outlook/history/summary", viewer(self._safe(self.summary))),
            web.post("/api/outlook/refresh", operator(self._safe(self.refresh))),
            web.get("/api/outlook/{id}", viewer(self._safe(self.one))),
            web.get("/api/outlook/{id}/mine", viewer(self._safe(self.mine))),
            web.put("/api/outlook/{id}/mine", viewer(self._safe(self.save_mine))),
            web.delete("/api/outlook/{id}/mine/scenarios/{key}", viewer(self._safe(self.reset_scenario))),
        ]

    def _safe(self, handler: Handler) -> Handler:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            if self.bot.outlook is None:
                if request.path == "/api/outlook/latest":
                    return web.json_response({"enabled": False, "reason": OFF})
                return web.json_response({"ok": False, "error": OFF}, status=404)
            try:
                return await handler(request)
            except OutlookError as exc:
                return web.json_response({"ok": False, "error": str(exc), "errors": exc.errors}, status=exc.status)

        return wrapped

    async def latest(self, request: web.Request) -> web.Response:
        wanted = request.query.get("instrument")
        if wanted and wanted.upper() != self.bot.outlook.cfg.instrument:
            raise OutlookError(f"no outlook for {wanted[:20]}; this bot follows {self.bot.outlook.cfg.instrument}", 404)
        return web.json_response(self.bot.outlook.latest(), dumps=_dumps)

    async def one(self, request: web.Request) -> web.Response:
        return web.json_response(self.bot.outlook.get(request.match_info["id"]), dumps=_dumps)

    async def history(self, request: web.Request) -> web.Response:
        query = request.query
        data = self.bot.outlook.history(self._user_of(request), query.get("from") or None, query.get("to") or None, query.get("session") or None)
        return web.json_response(data, dumps=_dumps)

    async def summary(self, request: web.Request) -> web.Response:
        query = request.query
        return web.json_response(self.bot.outlook.summary(self._user_of(request), query.get("from") or None, query.get("to") or None), dumps=_dumps)

    async def refresh(self, request: web.Request) -> web.Response:
        result = await self.bot.outlook.refresh(self._user_of(request))
        return web.json_response(result, status=200 if result.get("ok") else 502, dumps=_dumps)

    async def mine(self, request: web.Request) -> web.Response:
        return web.json_response(self.bot.outlook.mine(self._user_of(request), request.match_info["id"]), dumps=_dumps)

    async def save_mine(self, request: web.Request) -> web.Response:
        try:
            body = await request.json() if request.can_read_body else {}
        except ValueError:
            raise OutlookError("body must be JSON") from None
        if not isinstance(body, dict):
            raise OutlookError("body must be a JSON object")
        saved = self.bot.outlook.save_mine(self._user_of(request), request.match_info["id"], body)
        return web.json_response({"ok": True, **saved}, dumps=_dumps)

    async def reset_scenario(self, request: web.Request) -> web.Response:
        saved = self.bot.outlook.reset_scenario(self._user_of(request), request.match_info["id"], request.match_info["key"])
        return web.json_response({"ok": True, **saved}, dumps=_dumps)
