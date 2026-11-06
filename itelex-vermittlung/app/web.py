"""HTTP-API und Oberfläche der Vermittlung.

Als Home-Assistant-Add-on nur über Ingress erreichbar (Home Assistant prüft die Anmeldung).
Eigenständig (Docker) mit eigenem Passwort und Sitzungs-Cookie.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time

from aiohttp import web

import tnsproto as T
from exchange import Exchange, is_private
from store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")
ALLOWED = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]
COOKIE = "vx_sitzung"
SESSION_DAYS = 30
OPEN_PATHS = {"/login", "/api/login", "/favicon.ico"}


class Auth:
    def __init__(self, password: str, data_dir: str) -> None:
        self.password = password
        path = os.path.join(data_dir, "sitzung.key")
        try:
            with open(path, "rb") as f:
                self.key = f.read()
        except FileNotFoundError:
            self.key = b""
        if len(self.key) < 32:
            self.key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self.key)
        self._fails: dict[str, list[float]] = {}

    def _sig(self, payload: str) -> str:
        # Passwort fließt ein: ändert es sich, sind alle alten Sitzungen ungültig
        return hmac.new(self.key, (payload + "|" + self.password).encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"admin.{int(time.time()) + SESSION_DAYS * 86400}"
        return payload + "." + self._sig(payload)

    def valid(self, token: str | None) -> bool:
        if not token or token.count(".") != 2:
            return False
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(payload)):
            return False
        try:
            return int(payload.split(".")[1]) > time.time()
        except ValueError:
            return False

    def blocked(self, ip: str) -> bool:
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < 600]
        self._fails[ip] = fails
        return len(fails) >= 10

    def failed(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())


def public(entry: dict, ex: Exchange, online: dict) -> dict:
    e = dict(entry)
    c = online.get(e["number"])
    e["online"] = bool(c)
    e["online_since"] = c.since if c else None
    e["online_ip"] = c.ip if c else ""
    e["line_state"] = c.state if c else ""
    e["ext_code_str"] = f"{e['ext_code']:02d}" if e["ext_code"] else ""
    e["listed"] = ex.peer_reply(entry) is not None
    e["umleitung_aktiv"] = ex.redirect_target(entry)
    e["intern"] = is_private(e["number"])
    return e


async def check_conflict(ex: Exchange, data: dict, original: str | None) -> web.Response | None:
    """Warnung, wenn eine neue Nummer im öffentlichen Netz schon vergeben ist (409).
    Mit "trotzdem": true wird trotzdem gespeichert."""
    number = str(data.get("number", "")).strip()
    if not number.isdigit() or number == original or data.get("trotzdem"):
        return None
    hit = await ex.public_conflict(number)
    if not hit:
        return None
    return web.json_response({"conflict": True, "number": number, "name": hit.get("name") or "",
                              "quelle": hit["quelle_text"],
                              "error": f"Die Nummer {number} ist im öffentlichen Netz schon vergeben "
                                       f"({hit.get('name') or 'ohne Namen'})."}, status=409)


def book_row(rec: dict, quelle: str, own: dict | None = None) -> dict:
    from protocol import decode_extension
    return {"number": str(rec["number"]), "name": rec.get("name") or "",
            "typ": T.TYPE_NAMES.get(rec.get("type"), str(rec.get("type"))),
            "adresse": T.address_of(rec), "port": rec.get("port") or 0,
            "durchwahl": decode_extension(int(rec.get("extension") or 0)) or "",
            "stand": rec.get("timestamp") or 0, "quelle": quelle,
            "eigen": bool(own), "mode": own["mode"] if own else "",
            "umleitung": Exchange.redirect_target(own) if own else ""}


def make_app(ex: Exchange, store: Store, settings: dict, abgleich=None, auth: dict | None = None,
             rundsenden=None) -> web.Application:
    guard_auth = Auth(auth["password"], auth["data"]) if auth else None

    @web.middleware
    async def guard(request: web.Request, handler):
        if guard_auth:
            if request.path not in OPEN_PATHS and not guard_auth.valid(request.cookies.get(COOKIE)):
                if request.path.startswith("/api/"):
                    return web.json_response({"error": "Bitte anmelden"}, status=401)
                raise web.HTTPFound("login")
        elif os.environ.get("ITELEX_ALLOW_ALL") != "1":
            try:
                ok = any(ipaddress.ip_address(request.remote or "") in n for n in ALLOWED)
            except ValueError:
                ok = False
            if not ok:
                raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar")
        try:
            resp = await handler(request)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if request.path.startswith("/api/") or request.path in ("/", "/login"):
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    app = web.Application(middlewares=[guard])
    r = app.router

    async def index(_):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    async def login_page(_):
        return web.FileResponse(os.path.join(STATIC, "login.html"))

    async def login(request):
        ip = request.remote or "?"
        if guard_auth.blocked(ip):
            return web.json_response({"error": "Zu viele Versuche, bitte 10 Minuten warten"}, status=429)
        data = await request.json()
        if not hmac.compare_digest(str(data.get("password", "")).encode(), guard_auth.password.encode()):
            guard_auth.failed(ip)
            await asyncio.sleep(1)
            return web.json_response({"error": "Passwort falsch"}, status=403)
        resp = web.json_response({"ok": True})
        resp.set_cookie(COOKIE, guard_auth.issue(), max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="Strict", secure=request.secure, path="/")
        return resp

    async def logout(_):
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def status(_):
        day = time.time() - 86400
        return web.json_response({**ex.status(), **settings, "stats24h": store.stats(day),
                                  "count": len(store.teilnehmer()), "server_pin": bool(ex.cfg.server_pin),
                                  "login": bool(guard_auth),
                                  "abgleich": abgleich.status() if abgleich else None})

    async def teilnehmer(_):
        online = dict(ex.clients)
        return web.json_response([public(t, ex, online) for t in store.teilnehmer() if t["mode"] != "gruppe"])

    async def save(request):
        data = await request.json()
        original = request.match_info.get("number")
        old = store.get(original) if original else None
        if data.get("mode") == "gruppe" or (old and old["mode"] == "gruppe"):
            raise ValueError("Gruppen werden unter „Rundsenden“ verwaltet")
        conflict = await check_conflict(ex, data, original)
        if conflict is not None:
            return conflict
        entry = store.save(data, original=original)
        if original and original != entry["number"]:
            c = ex.clients.get(original)
            if c:
                ex._drop(c)
        if entry["mode"] != "vermittlung" and entry["number"] in ex.clients:
            ex._drop(ex.clients[entry["number"]])
        if abgleich:
            abgleich.soon()
        return web.json_response(public(entry, ex, dict(ex.clients)))

    async def delete(request):
        number = request.match_info["number"]
        if number in ex.clients:
            ex._drop(ex.clients[number])
        store.delete(number)
        if abgleich:
            abgleich.soon()
        return web.json_response({"ok": True})

    async def umleitung(request):
        number = request.match_info["number"]
        entry = store.get(number)
        if not entry:
            raise ValueError("Unbekannter Teilnehmer")
        data = await request.json()
        ziel = str(data.get("ziel", "") or "").strip()
        bis = float(data.get("bis") or 0)
        if ziel and (not ziel.isdigit() or not 3 <= len(ziel) <= 9 or ziel == number):
            raise ValueError("Zielnummer: 3 bis 9 Ziffern, nicht die eigene")
        if bis and bis <= time.time():
            raise ValueError("Das Ende der Umleitung liegt in der Vergangenheit")
        if ziel:
            rec, src = await ex.lookup(ziel)
            if not rec:
                raise ValueError(f"Zielnummer {ziel} ist nicht erreichbar ({src})")
        store.set_umleitung(number, ziel, bis)
        store.log("umleitung", number=number, detail=f"auf {ziel}" if ziel else "aufgehoben")
        if abgleich:
            abgleich.soon()
        return web.json_response(public(store.get(number), ex, dict(ex.clients)))

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

    async def telefonbuch(request):
        q = request.query.get("q", "").strip().lower()
        limit = min(int(request.query.get("limit", 300)), 2000)
        rows: dict[str, dict] = {}
        for vz in store.vz_all():
            if vz["type"] == T.T_DELETED or vz["disabled"]:
                continue
            rows[vz["number"]] = book_row(vz, "Abgleich" if vz["quelle"] == "abgleich" else "öffentlich")
        for entry in store.teilnehmer():
            rec = ex.own_record(entry, publish=True) or {"number": entry["number"], "name": entry["name"]}
            rows[entry["number"]] = book_row(rec, "eigener Teilnehmer", entry)
        items = sorted(rows.values(), key=lambda x: int(x["number"]))
        if q:
            words = q.split()
            items = [i for i in items
                     if all(w in (i["number"] + " " + i["name"] + " " + i["adresse"]).lower() for w in words)]
        return web.json_response({"total": len(rows), "count": len(items), "items": items[:limit]})

    async def pruefen(request):
        number = request.match_info["number"]
        if not number.isdigit():
            raise ValueError("Nummer: nur Ziffern")
        rec, src = await ex.lookup(number)
        return web.json_response({"found": bool(rec), "quelle": src,
                                  "eintrag": book_row(rec, src) if rec else None})

    # --- Rundsenden ----------------------------------------------------------------
    def gruppe_out(g: dict) -> dict:
        g = dict(g)
        g["ext_code_str"] = f"{g['ext_code']:02d}" if g["ext_code"] else ""
        g["listed"] = ex.peer_reply(g) is not None
        g["intern"] = is_private(g["number"])
        return g

    async def gruppen(_):
        return web.json_response([gruppe_out(g) for g in store.gruppen()])

    async def gruppe_save(request):
        original = request.match_info.get("number")
        data = await request.json()
        conflict = await check_conflict(ex, data, original)
        if conflict is not None:
            return conflict
        g = store.gruppe_save(data, original=original)
        if abgleich:
            abgleich.soon()
        return web.json_response(gruppe_out(g))

    async def gruppe_delete(request):
        number = request.match_info["number"]
        if not store.gruppe(number):
            raise ValueError("Unbekannte Gruppe")
        store.delete(number)
        if abgleich:
            abgleich.soon()
        return web.json_response({"ok": True})

    async def mitglieder(request):
        number = request.match_info["number"]
        if not store.gruppe(number):
            raise ValueError("Unbekannte Gruppe")
        return web.json_response(store.mitglieder(number))

    async def mitglied_add(request):
        number = request.match_info["number"]
        if not store.gruppe(number):
            raise ValueError("Unbekannte Gruppe")
        data = await request.json()
        added = []
        for m in str(data.get("number", "")).replace(";", ",").replace(" ", ",").split(","):
            m = m.strip()
            if not m:
                continue
            name = str(data.get("name", "") or "").strip()
            if not name:
                rec, _ = await ex.lookup(m)
                name = (rec or {}).get("name", "")
            store.mitglied_add(number, m, name)
            added.append(m)
        if not added:
            raise ValueError("Bitte eine Nummer angeben")
        return web.json_response(store.mitglieder(number))

    async def mitglied_del(request):
        store.mitglied_del(request.match_info["number"], request.match_info["m"])
        return web.json_response(store.mitglieder(request.match_info["number"]))

    async def gruppe_senden(request):
        if not rundsenden:
            raise ValueError("Rundsenden nicht verfügbar")
        data = await request.json()
        rid = rundsenden.broadcast(request.match_info["number"], str(data.get("text", "")), quelle="verwaltung")
        return web.json_response({"id": rid})

    async def rs_list(request):
        return web.json_response(store.rs_list(int(request.query.get("limit", 50)),
                                               request.query.get("gruppe", "")))

    async def rs_get(request):
        rs = store.rs_get(int(request.match_info["rid"]))
        if not rs:
            raise ValueError("Unbekannte Rundsendung")
        return web.json_response(rs)

    async def abgleich_jetzt(_):
        if not abgleich:
            raise ValueError("Abgleich nicht verfügbar")
        abgleich.now()
        return web.json_response({"ok": True})

    r.add_get("/", index)
    r.add_get("/login", login_page)
    if guard_auth:
        r.add_post("/api/login", login)
        r.add_post("/api/logout", logout)
    r.add_get("/api/status", status)
    r.add_get("/api/teilnehmer", teilnehmer)
    r.add_post("/api/teilnehmer", save)
    r.add_put("/api/teilnehmer/{number}", save)
    r.add_delete("/api/teilnehmer/{number}", delete)
    r.add_post("/api/teilnehmer/{number}/trennen", disconnect)
    r.add_post("/api/teilnehmer/{number}/umleitung", umleitung)
    r.add_get("/api/events", events)
    r.add_get("/api/telefonbuch", telefonbuch)
    r.add_get("/api/pruefen/{number}", pruefen)
    r.add_post("/api/abgleich/jetzt", abgleich_jetzt)
    r.add_get("/api/gruppen", gruppen)
    r.add_post("/api/gruppen", gruppe_save)
    r.add_put("/api/gruppen/{number}", gruppe_save)
    r.add_delete("/api/gruppen/{number}", gruppe_delete)
    r.add_get("/api/gruppen/{number}/mitglieder", mitglieder)
    r.add_post("/api/gruppen/{number}/mitglieder", mitglied_add)
    r.add_delete("/api/gruppen/{number}/mitglieder/{m}", mitglied_del)
    r.add_post("/api/gruppen/{number}/senden", gruppe_senden)
    r.add_get("/api/rundsendungen", rs_list)
    r.add_get("/api/rundsendungen/{rid}", rs_get)
    return app
