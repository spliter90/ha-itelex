"""HTTP-API und Oberfläche der Vermittlung (Home-Assistant-Ingress)."""

from __future__ import annotations

import ipaddress
import os
import time

from aiohttp import web

from exchange import Exchange
from store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")
ALLOWED = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]


@web.middleware
async def guard(request: web.Request, handler):
    if os.environ.get("ITELEX_ALLOW_ALL") != "1":
        try:
            ok = any(ipaddress.ip_address(request.remote or "") in n for n in ALLOWED)
        except ValueError:
            ok = False
        if not ok:
            raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar")
    try:
        return await handler(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


def public(entry: dict, ex: Exchange, online: dict) -> dict:
    e = dict(entry)
    c = online.get(e["number"])
    e["online"] = bool(c)
    e["online_since"] = c.since if c else None
    e["online_ip"] = c.ip if c else ""
    e["line_state"] = c.state if c else ""
    e["ext_code_str"] = f"{e['ext_code']:02d}" if e["ext_code"] else ""
    e["listed"] = ex.peer_reply(entry) is not None
    return e


def make_app(ex: Exchange, store: Store, settings: dict) -> web.Application:
    app = web.Application(middlewares=[guard])
    r = app.router

    async def index(_):
        return web.FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})

    async def status(_):
        day = time.time() - 86400
        return web.json_response({**ex.status(), **settings, "stats24h": store.stats(day),
                                  "count": len(store.teilnehmer())})

    async def teilnehmer(_):
        online = dict(ex.clients)
        return web.json_response([public(t, ex, online) for t in store.teilnehmer()])

    async def save(request):
        data = await request.json()
        original = request.match_info.get("number")
        entry = store.save(data, original=original)
        if original and original != entry["number"]:
            c = ex.clients.get(original)
            if c:
                ex._drop(c)
        if entry["mode"] != "vermittlung" and entry["number"] in ex.clients:
            ex._drop(ex.clients[entry["number"]])
        return web.json_response(public(entry, ex, dict(ex.clients)))

    async def delete(request):
        number = request.match_info["number"]
        if number in ex.clients:
            ex._drop(ex.clients[number])
        store.delete(number)
        return web.json_response({"ok": True})

    async def disconnect(request):
        number = request.match_info["number"]
        c = ex.clients.get(number)
        if not c:
            raise ValueError("Station ist nicht angemeldet")
        ex._drop(c)
        return web.json_response({"ok": True})

    async def events(request):
        return web.json_response(store.events(int(request.query.get("limit", 300)),
                                              request.query.get("kind", "")))

    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_get("/api/teilnehmer", teilnehmer)
    r.add_post("/api/teilnehmer", save)
    r.add_put("/api/teilnehmer/{number}", save)
    r.add_delete("/api/teilnehmer/{number}", delete)
    r.add_post("/api/teilnehmer/{number}/trennen", disconnect)
    r.add_get("/api/events", events)
    return app
