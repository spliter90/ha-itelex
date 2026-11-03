"""Anbindung an Home Assistant über die Supervisor-API (Events + Sensoren)."""

from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("itelex.hass")

API = "http://supervisor/core/api"


class Hass:
    def __init__(self) -> None:
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _post(self, path: str, payload: dict) -> None:
        if not self.enabled:
            return
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self._session.post(API + path, json=payload) as r:
                if r.status >= 400:
                    log.warning("HA-API %s: HTTP %s", path, r.status)
        except aiohttp.ClientError as e:
            log.warning("HA-API %s nicht erreichbar: %s", path, e)

    async def fire(self, event: str, data: dict) -> None:
        await self._post(f"/events/{event}", data)

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        await self._post(f"/states/{entity_id}", {"state": state, "attributes": attributes})

    async def publish(self, store, active_calls: int, accepting: bool) -> None:
        last = store.last_incoming()
        unread = store.unread_count()
        await self.set_state("sensor.itelex_ungelesen", unread, {
            "friendly_name": "Telex ungelesen",
            "icon": "mdi:email-alert" if unread else "mdi:email-check",
            "unit_of_measurement": "Nachrichten",
        })
        if last:
            name = last["peer_name"] or last["answerback"] or last["peer"]
            text = last["text"]
            await self.set_state("sensor.itelex_letzte_nachricht", (text[:250] or "-"), {
                "friendly_name": "Telex letzte Nachricht",
                "icon": "mdi:printer-pos",
                "absender": last["peer"],
                "name": name,
                "kennung": last["answerback"],
                "zeit": last["ts"],
                "text": text,
                "vorlesen": f"Letztes Telex von {name}: {text[:600]}",
            })
        await self.set_state("binary_sensor.itelex_verbindung", "on" if active_calls else "off", {
            "friendly_name": "Telex Verbindung",
            "device_class": "connectivity",
            "aktive_verbindungen": active_calls,
            "annahme": accepting,
        })

    async def close(self) -> None:
        if self._session:
            await self._session.close()
