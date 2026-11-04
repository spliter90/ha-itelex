"""Einstiegspunkt der Vermittlung: liest /data/options.json, startet Dienste und Oberfläche."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import aiohttp
from aiohttp import web

from exchange import Config, Exchange
from store import Store
from web import make_app

DATA = os.environ.get("ITELEX_DATA", "/data")
WEB_PORT = int(os.environ.get("ITELEX_WEB_PORT", "8099"))
RETENTION_DAYS = 30

logging.basicConfig(level=os.environ.get("ITELEX_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger("vermittlung")


def load() -> tuple[Config, dict]:
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as f:
            opts = json.load(f)
    except FileNotFoundError:
        opts = {}
    cfg = Config()
    cfg.public_host = str(opts.get("oeffentliche_adresse", "") or "").strip()
    cfg.public_port = int(opts.get("vermittlung_port_extern", 49491))
    cfg.forward = bool(opts.get("unbekannte_nummern_weiterleiten", True))
    if opts.get("oeffentliche_teilnehmerserver"):
        cfg.upstream = list(opts["oeffentliche_teilnehmerserver"])
    cfg.tns_port = int(os.environ.get("ITELEX_TNS_PORT", 11811))
    cfg.vx_port = int(os.environ.get("ITELEX_VX_PORT", 49491))
    if os.environ.get("ITELEX_UPSTREAM_PORT"):
        cfg.upstream_port = int(os.environ["ITELEX_UPSTREAM_PORT"])
    level = str(opts.get("protokoll", "info")).upper()
    if not os.environ.get("ITELEX_LOG"):
        logging.getLogger().setLevel(level if level in ("DEBUG", "INFO", "WARNING") else "INFO")
    settings = {"public_host": cfg.public_host, "public_port": cfg.public_port, "forward": cfg.forward,
                "upstream": cfg.upstream, "retention_days": RETENTION_DAYS,
                "log_level": level.lower()}
    return cfg, settings


async def main() -> None:
    cfg, settings = load()
    store = Store(os.path.join(DATA, "vermittlung.db"))
    token = os.environ.get("SUPERVISOR_TOKEN")
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
    app = make_app(ex, store, settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("Oberfläche auf Port %s; öffentliche Adresse: %s:%s; Weiterleitung ans öffentliche Netz: %s",
             WEB_PORT, cfg.public_host or "(nicht gesetzt)", cfg.public_port, "an" if cfg.forward else "aus")
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
    await ex.stop()
    await runner.cleanup()
    if session:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
