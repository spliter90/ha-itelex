"""Einstiegspunkt: liest /data/options.json, startet Station, Web und Wartung."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from aiohttp import web

from hass import Hass
from station import Config, Station
from store import Store
from web import make_app

DATA = os.environ.get("ITELEX_DATA", "/data")
WEB_PORT = int(os.environ.get("ITELEX_WEB_PORT", "8099"))

logging.basicConfig(level=os.environ.get("ITELEX_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger("itelex")


def load_config() -> Config:
    cfg = Config()
    try:
        with open(os.path.join(DATA, "options.json"), encoding="utf-8") as f:
            opts = json.load(f)
    except FileNotFoundError:
        opts = {}
    cfg.number = str(opts.get("nummer", "") or "")
    cfg.answerback = str(opts.get("kennung", "") or "")
    cfg.port = int(os.environ.get("ITELEX_PORT", opts.get("port", 134)))
    if opts.get("tns_server"):
        cfg.tns_servers = list(opts["tns_server"])
    cfg.identify_caller = bool(opts.get("anrufer_kennung_abfragen", True))
    cfg.confirm_delivery = bool(opts.get("zustellung_bestaetigen", True))
    cfg.retention_days = int(opts.get("aufbewahrung_tage", 90))
    cfg.linger = int(opts.get("auflegen_nach_senden", 300))
    cfg.idle_in = int(opts.get("auflegen_eingehend", 300))
    cfg.tns_dynip = bool(opts.get("dynamische_ip", False))
    cfg.tns_pin = int(opts.get("tns_pin", 0) or 0)
    cfg.public_port = int(opts.get("oeffentlicher_port", 134))
    cfg.vx_host = str(opts.get("vermittlung", "") or "").strip()
    cfg.vx_port = int(opts.get("vermittlung_port", 49491))
    level = str(opts.get("protokoll", "info")).upper()
    if not os.environ.get("ITELEX_LOG"):
        logging.getLogger().setLevel(level if level in ("DEBUG", "INFO", "WARNING") else "INFO")
    return cfg


async def main() -> None:
    cfg = load_config()
    store = Store(os.path.join(DATA, "itelex.db"))
    hass = Hass()
    station: Station

    async def on_change(event: str, data: dict) -> None:
        if event == "received":
            await hass.fire("itelex_nachricht", {
                "absender": data.get("peer"), "name": data.get("name", ""),
                "kennung": data.get("answerback", ""), "text": data.get("text", "")})
        if event in ("received", "read", "deleted", "call", "settings", "sent"):
            await hass.publish(store, station.active, station.accepting)
        if event == "sent" and data.get("id"):
            m = store.message(data["id"])
            if m:
                await hass.fire("itelex_gesendet", {
                    "empfaenger": m["peer"], "status": m["status"], "detail": m["detail"]})

    station = Station(cfg, store, on_change)
    await station.start()

    app = make_app(station, store)
    app["notify"] = on_change
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("Weboberfläche auf Port %s, Aufbewahrung %s Tage, auflegen nach %s s (gesendet) / %s s (eingehend)",
             WEB_PORT, station.retention_days, cfg.linger, cfg.idle_in)
    if cfg.tns_dynip and not (cfg.number.isdigit() and cfg.tns_pin):
        log.warning("Dynamische IP ist an, aber Nummer oder TNS-PIN fehlt; keine Aktualisierung")
    await hass.publish(store, 0, station.accepting)

    async def maintenance() -> None:
        while True:
            days = station.retention_days
            n = store.purge(days)
            if n:
                log.info("%s Nachrichten älter als %s Tage gelöscht", n, days)
                await hass.publish(store, station.active, station.accepting)
            await station.refresh_blocklist()
            await station.tns_update()
            await asyncio.sleep(3600)

    asyncio.create_task(maintenance())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await station.stop()
    await runner.cleanup()
    await hass.close()


if __name__ == "__main__":
    asyncio.run(main())
