"""HTTP-API und Weboberfläche (Home-Assistant-Ingress, Port 8099)."""

from __future__ import annotations

import ipaddress
import logging
import os
import time

from aiohttp import web

import tns
from station import RETENTION_CHOICES, Station
from store import Store

log = logging.getLogger("itelex.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")

# Ingress (172.30.32.2), HA Core (172.30.32.1) und lokal
ALLOWED_NETS = [ipaddress.ip_network(n) for n in
                ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]


def _allowed(remote: str | None) -> bool:
    if os.environ.get("ITELEX_ALLOW_ALL") == "1":
        return True
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    return any(ip in n for n in ALLOWED_NETS)


@web.middleware
async def guard(request: web.Request, handler):
    if not _allowed(request.remote):
        raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar")
    try:
        return await handler(request)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


def make_app(station: Station, store: Store) -> web.Application:
    app = web.Application(middlewares=[guard])
    r = app.router

    async def index(_):
        return web.FileResponse(os.path.join(STATIC, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    async def status(_):
        return web.json_response({
            "number": station.cfg.number,
            "answerback": station.cfg.answerback,
            "port": station.cfg.port,
            "accepting": station.accepting,
            "active": station.active,
            "queued": station.queue.qsize(),
            "unread": store.unread_count(),
            "retention_days": station.retention_days,
            "sessions": station.session_info(),
            "linger": station.linger, "idle_in": station.cfg.idle_in,
            "tns": station.tns_status, "public_port": station.cfg.public_port,
            "log_level": logging.getLevelName(logging.getLogger().level).lower(),
            "time": time.time(),
        })

    async def conversations(_):
        return web.json_response(store.conversations())

    async def messages(request):
        peer = request.query.get("peer", "")
        return web.json_response(store.messages(peer))

    async def mark_read(request):
        data = await request.json()
        store.mark_read(str(data.get("peer", "")))
        await notify("read", {})
        return web.json_response({"ok": True})

    async def delete_conv(request):
        store.delete_conversation(request.match_info["peer"])
        await notify("deleted", {})
        return web.json_response({"ok": True})

    async def send(request):
        data = await request.json()
        to = str(data.get("to", "")).strip()
        text = str(data.get("text", ""))
        if to and not to.replace(" ", "").isdigit() and ":" not in to:
            found = store.find_peer_by_name(to)
            if not found:
                raise ValueError(f"Kein Kontakt namens '{to}' bekannt")
            to = found
        mid = station.enqueue(to, text, greet=bool(data.get("greet", True)),
                              start_line=data.get("start_line"), end_line=data.get("end_line"))
        return web.json_response({"id": mid, "to": to})

    async def sessions(_):
        return web.json_response(station.session_info())

    async def end_session(request):
        if not station.end_session(int(request.match_info["sid"])):
            raise ValueError("Keine offene Verbindung")
        return web.json_response({"ok": True})

    async def extend_session(request):
        if not station.extend_session(int(request.match_info["sid"])):
            raise ValueError("Keine offene Verbindung")
        return web.json_response({"ok": True})

    async def session_wru(request):
        ab = await station.session_wru(int(request.match_info["sid"]))
        return web.json_response({"answerback": ab})

    async def keep(request):
        data = await request.json()
        peer = request.match_info["peer"]
        store.set_kept(peer, bool(data.get("keep", True)))
        return web.json_response({"kept": store.is_kept(peer)})

    async def export(request):
        peer = request.query.get("peer", "")
        msgs = store.messages(peer, limit=100000)
        conv = next((c for c in store.conversations() if c["peer"] == peer), {})
        name = conv.get("name") or conv.get("answerback") or peer
        stamp = lambda ts: time.strftime("%d.%m.%y %H:%M", time.localtime(ts))
        lines = [f"TELEX-VERLAUF {name}" + (f" ({peer})" if name != peer else ""),
                 f"Gespeichert am {time.strftime('%d.%m.%Y %H:%M')}"
                 + (f", eigene Kennung {station.cfg.answerback.upper()}" if station.cfg.answerback else ""),
                 "=" * 68, ""]
        for m in msgs:
            if m["direction"] == "in":
                head = f"{stamp(m['ts'])}  EINGANG" + (f"  Kennung: {m['answerback']}" if m["answerback"] else "")
                body = m["text"]
            else:
                st = {"delivered": "zugestellt", "sent": "übertragen", "failed": "fehlgeschlagen",
                      "queued": "in Warteschlange", "sending": "wird gesendet", "retry": "wird wiederholt"}
                head = f"{stamp(m['ts'])}  AUSGANG  {st.get(m['status'], m['status'])}"
                body = m["text"].upper()
            lines += [head, body, "-" * 68, ""]
        safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "-_")[:40] or "verlauf"
        fname = f"telex-{safe}-{time.strftime('%Y%m%d')}.txt"
        return web.Response(text="\n".join(lines), content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    async def calls(_):
        rows = store.calls()
        for c in rows:
            c["contact"] = store.contact_name(c["peer"])
        return web.json_response(rows)

    async def contacts(_):
        return web.json_response(store.contacts())

    async def save_contact(request):
        data = await request.json()
        cid = store.save_contact(str(data.get("number", "")), str(data.get("name", "")),
                                 str(data.get("note", "")), data.get("id") or None)
        return web.json_response({"id": cid})

    async def delete_contact(request):
        store.delete_contact(int(request.match_info["cid"]))
        return web.json_response({"ok": True})

    async def get_settings(_):
        return web.json_response({"accepting": station.accepting, **station.greetings(),
                                  "retention_days": station.retention_days,
                                  "retention_choices": list(RETENTION_CHOICES),
                                  "linger": station.linger, "idle_in": station.cfg.idle_in,
                                  "placeholders": ["{datum}", "{zeit}", "{name}", "{nummer}", "{kennung}"]})

    async def preview(request):
        data = await request.json()
        from station import render_greeting
        number = str(data.get("number", "12345"))
        name = str(data.get("name") or store.contact_name(number) or ("Muster GmbH" if "name" not in data else ""))
        return web.json_response({"text": render_greeting(
            str(data.get("template", "")), name=name, number=number, own=station.cfg.answerback)})

    async def blocklist(_):
        return web.json_response(store.blocklist())

    async def block(request):
        data = await request.json()
        bid = await station.block(str(data.get("pattern", "")), str(data.get("note", "")))
        return web.json_response({"id": bid})

    async def unblock(request):
        store.unblock(int(request.match_info["bid"]))
        return web.json_response({"ok": True})

    async def settings(request):
        data = await request.json()
        if "accepting" in data:
            station.set_accepting(bool(data["accepting"]))
            await notify("settings", {})
        station.set_greetings(data)
        removed = 0
        if "retention_days" in data:
            removed = station.set_retention(int(data["retention_days"]))
            await notify("deleted", {})
        return web.json_response({"accepting": station.accepting, **station.greetings(),
                                  "retention_days": station.retention_days, "linger": station.linger,
                                  "removed": removed})

    async def lookup(request):
        number = request.query.get("number", "").strip()
        try:
            peer = await tns.lookup(number, station.cfg.tns_servers, station.cfg.tns_port)
        except tns.TNSError as e:
            return web.json_response({"error": str(e)}, status=502)
        if not peer:
            return web.json_response({"found": False})
        return web.json_response({"found": True, "number": peer.number, "name": peer.name,
                                  "ascii": peer.ascii, "extension": peer.extension})

    async def notify(event, data):
        cb = app.get("notify")
        if cb:
            await cb(event, data)

    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_get("/api/conversations", conversations)
    r.add_get("/api/messages", messages)
    r.add_post("/api/read", mark_read)
    r.add_delete("/api/conversations/{peer}", delete_conv)
    r.add_post("/api/send", send)
    r.add_get("/api/calls", calls)
    r.add_post("/api/conversations/{peer}/keep", keep)
    r.add_get("/api/export", export)
    r.add_get("/api/sessions", sessions)
    r.add_post("/api/sessions/{sid}/end", end_session)
    r.add_post("/api/sessions/{sid}/wru", session_wru)
    r.add_post("/api/sessions/{sid}/extend", extend_session)
    r.add_get("/api/blocklist", blocklist)
    r.add_post("/api/blocklist", block)
    r.add_delete("/api/blocklist/{bid}", unblock)
    r.add_get("/api/settings", get_settings)
    r.add_post("/api/settings", settings)
    r.add_post("/api/preview", preview)
    r.add_get("/api/contacts", contacts)
    r.add_post("/api/contacts", save_contact)
    r.add_delete("/api/contacts/{cid}", delete_contact)
    r.add_get("/api/lookup", lookup)
    r.add_static("/static", STATIC)
    return app
