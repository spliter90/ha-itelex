"""Einstiegspunkt der Vermittlung: liest die Einstellungen, startet Dienste und Oberfläche.

Als Home-Assistant-Add-on kommen die Einstellungen aus /data/options.json. Im eigenständigen
Docker-Betrieb aus Umgebungsvariablen (ITELEX_*), die auch im Add-on Vorrang hätten.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal

import aiohttp
from aiohttp import web

from exchange import Config, Exchange, normalize_server
from store import Store
from sync import Abgleich
from web import make_app

DATA = os.environ.get("ITELEX_DATA", "/data")
WEB_PORT = int(os.environ.get("ITELEX_WEB_PORT", "8099"))
RETENTION_DAYS = 30

logging.basicConfig(level=os.environ.get("ITELEX_LOG", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger("vermittlung")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else None


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def _bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "ja", "yes", "on")


def load() -> tuple[Config, dict]:
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as f:
            opts = json.load(f)
    except FileNotFoundError:
        opts = {}
    env = {
        "oeffentliche_adresse": _env("ITELEX_PUBLIC_HOST"),
        "vermittlung_port_extern": _env("ITELEX_PUBLIC_PORT"),
        "unbekannte_nummern_weiterleiten": _env("ITELEX_FORWARD"),
        "oeffentliche_teilnehmerserver": _env("ITELEX_UPSTREAM"),
        "server_pin": _env("ITELEX_SERVER_PIN"),
        "abgleich_server": _env("ITELEX_SYNC_SERVERS"),
        "telefonbuch_stunden": _env("ITELEX_PHONEBOOK_HOURS"),
        "protokoll": _env("ITELEX_LOG"),
    }
    opts.update({k: v for k, v in env.items() if v is not None})

    cfg = Config()
    cfg.public_host = str(opts.get("oeffentliche_adresse", "") or "").strip()
    cfg.public_port = int(opts.get("vermittlung_port_extern", 49491) or 49491)
    cfg.forward = _bool(opts.get("unbekannte_nummern_weiterleiten", True))
    if _list(opts.get("oeffentliche_teilnehmerserver")):
        cfg.upstream = _list(opts["oeffentliche_teilnehmerserver"])
    cfg.tns_port = int(os.environ.get("ITELEX_TNS_PORT", 11811))
    cfg.vx_port = int(os.environ.get("ITELEX_VX_PORT", 49491))
    if os.environ.get("ITELEX_UPSTREAM_PORT"):
        cfg.upstream_port = int(os.environ["ITELEX_UPSTREAM_PORT"])
    pin = str(opts.get("server_pin", "") or "").strip()
    if pin and (not pin.isdigit() or int(pin) > 0xFFFFFFFF):
        log.error("Server-PIN muss eine Zahl bis 4294967295 sein – Abgleich bleibt aus")
        pin = ""
    cfg.server_pin = int(pin) if pin else 0
    cfg.sync_servers = [normalize_server(s) for s in _list(opts.get("abgleich_server"))] or \
        [normalize_server(s) for s in cfg.upstream]
    level = str(opts.get("protokoll", "info")).upper()
    logging.getLogger().setLevel(level if level in ("DEBUG", "INFO", "WARNING") else "INFO")
    settings = {"public_host": cfg.public_host, "public_port": cfg.public_port, "forward": cfg.forward,
                "upstream": cfg.upstream, "retention_days": RETENTION_DAYS,
                "log_level": level.lower(), "sync_servers": cfg.sync_servers,
                "phonebook_hours": float(opts.get("telefonbuch_stunden", 6) or 6)}
    return cfg, settings


def admin_password() -> str:
    """Passwort für die Oberfläche im eigenständigen Betrieb. Ohne ITELEX_ADMIN_PASSWORD wird beim
    ersten Start eines erzeugt und in /data/admin-passwort.txt abgelegt."""
    pw = _env("ITELEX_ADMIN_PASSWORD")
    if pw:
        return pw
    path = os.path.join(DATA, "admin-passwort.txt")
    try:
        with open(path, encoding="utf-8") as f:
            pw = f.read().strip()
    except FileNotFoundError:
        pw = ""
    if not pw:
        pw = secrets.token_urlsafe(12)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        log.warning("Kein ITELEX_ADMIN_PASSWORD gesetzt: Passwort für die Oberfläche erzeugt, "
                    "steht in %s", path)
    return pw


async def main() -> None:
    cfg, settings = load()
    store = Store(os.path.join(DATA, "vermittlung.db"))
    token = os.environ.get("SUPERVISOR_TOKEN")
    standalone = not token or os.environ.get("ITELEX_STANDALONE") == "1"
    settings["standalone"] = standalone
    session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"},
                                    timeout=aiohttp.ClientTimeout(total=10)) if token else None
    ex: Exchange

    async def publish() -> None:
        if not session:
            return
        online = sorted(ex.clients)
        try:
            async with session.post("http://supervisor/core/api/states/sensor.itelex_vermittlung", json={
                "state": len(online),
                "attributes": {"friendly_name": "i-Telex-Vermittlung angemeldet",
                               "icon": "mdi:phone-classic", "unit_of_measurement": "Stationen",
                               "stationen": online, "gespraeche": len(ex.calls),
                               "teilnehmer": len(store.teilnehmer())}}):
                pass
        except aiohttp.ClientError as e:
            log.debug("Home Assistant nicht erreichbar: %s", e)

    ex = Exchange(cfg, store, publish)
    await ex.start()
    abgleich = Abgleich(ex, store, settings["phonebook_hours"])
    abgleich.start()
    auth = {"password": admin_password(), "data": DATA} if standalone else None
    app = make_app(ex, store, settings, abgleich, auth)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("Oberfläche auf Port %s%s; öffentliche Adresse: %s:%s; Weiterleitung ans öffentliche Netz: %s",
             WEB_PORT, " (mit Anmeldung)" if auth else "", cfg.public_host or "(nicht gesetzt)",
             cfg.public_port, "an" if cfg.forward else "aus")
    if cfg.server_pin:
        log.info("Vollwertiger Teilnehmerserver, Abgleich mit: %s", ", ".join(cfg.sync_servers))
    else:
        log.info("Ohne Server-PIN: öffentliches Telefonbuch wird alle %.0f Stunden gespiegelt",
                 settings["phonebook_hours"])
    await publish()

    async def maintenance() -> None:
        while True:
            store.purge(RETENTION_DAYS)
            await asyncio.sleep(6 * 3600)

    asyncio.create_task(maintenance())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await abgleich.stop()
    await ex.stop()
    await runner.cleanup()
    if session:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
