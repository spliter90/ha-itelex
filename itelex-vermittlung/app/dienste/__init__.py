"""Anrufbare Dienste (Plugins).

Ein Plugin ist ein Modul in diesem Ordner mit zwei Dingen:

    DIENST = {"id": "burg", "name": "Die Burg", "beschreibung": "Textabenteuer ..."}

    async def run(s: Sitzung) -> None:
        await s.schreibe("Hallo")
        eingabe = await s.lies()          # nächste getippte Zeile, None = aufgelegt / Zeitüberschreitung
        s.speicher["zaehler"] = 1         # bleibt je Anrufer erhalten (wird nach run() gespeichert)

In der Oberfläche bekommt ein Plugin unter „Dienste“ eine Telexnummer und ist dann anrufbar wie
eine Station „über Vermittlung“. Mehrere Nummern dürfen dasselbe Plugin nutzen; der Speicher gilt
je Plugin und Anrufer. Text wird automatisch in Fernschreibschrift umgesetzt (Großbuchstaben,
Umlaute umschrieben, 68 Zeichen pro Zeile).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import re
import time

import protocol as P
import telex
from exchange import Exchange
from store import Store

log = logging.getLogger("vermittlung.dienste")
# Grußzeile, die Stationen (z. B. das i-Telex-Add-on) automatisch vor den Text setzen
DATUMSZEILE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4},? \d{1,2}[:.]\d{2}$")

IDLE = 10 * 60          # Sekunden ohne Eingabe -> Dienst wird beendet
MAX_CALL = 2 * 3600     # längster Anruf


def plugins() -> dict:
    """Alle Plugins in diesem Ordner: id -> Modul."""
    found = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:
            log.exception("Plugin %s lässt sich nicht laden", info.name)
            continue
        meta = getattr(mod, "DIENST", None)
        if isinstance(meta, dict) and meta.get("id") and callable(getattr(mod, "run", None)):
            found[meta["id"]] = mod
    return found


class Aufgelegt(Exception):
    """Anrufer hat aufgelegt (oder die Leitung ist weg)."""


class Sitzung:
    """Was ein Plugin von einem Anruf sieht."""

    def __init__(self, line: telex.Line, anrufer: str, kennung: str, speicher: dict, dienst: dict) -> None:
        self._line = line
        self.anrufer = anrufer                # Nummer aus der Kennung des Anrufers ("" = unbekannt)
        self.anrufer_kennung = kennung
        self.speicher = speicher              # je Plugin und Anrufer gespeichert
        self.dienst = dienst                  # Nummer, Name, Kennung des Dienstes
        self._pos = len(line.rx_text)
        self.started = time.monotonic()

    @property
    def verbunden(self) -> bool:
        return not self._line.closed.is_set()

    async def schreibe(self, text: str = "") -> None:
        """Text ausgeben (mit Zeilenumbruch am Ende)."""
        if not self.verbunden:
            raise Aufgelegt()
        try:
            await self._line.send_text(P.wrap(P.normalize(text)) + "\r\n")   # wrap liefert CR LF
        except telex.CallFailed as e:
            raise Aufgelegt() from e

    async def lies(self, timeout: float = IDLE) -> str | None:
        """Nächste eingegebene Zeile (Großbuchstaben, ohne Leerzeichen am Rand).
        None, wenn aufgelegt wurde oder lange nichts kam."""
        deadline = time.monotonic() + timeout
        line = self._line
        while True:
            text = line.rx_text
            for i in range(self._pos, len(text)):
                if text[i] in "\r\n":
                    raw = text[self._pos:i]
                    self._pos = i + 1
                    clean = telex.clean_answerback(raw)
                    if not clean or clean in line.answerbacks or DATUMSZEILE.match(clean):
                        break                  # leer, Kennung oder Datumszeile des Anrufers: weiter lesen
                    return clean.upper()
            else:
                if line.closed.is_set() or time.monotonic() > deadline:
                    rest = telex.clean_answerback(text[self._pos:])
                    self._pos = len(text)
                    if rest and line.closed.is_set():
                        return rest.upper()       # letzte Zeile ohne Zeilenende
                    return None
                await line._wait_progress(1.0)
                continue

    async def pause(self, sekunden: float) -> None:
        await asyncio.sleep(sekunden)


class Dienste:
    def __init__(self, ex: Exchange, store: Store) -> None:
        self.ex = ex
        self.store = store
        self.plugins = plugins()
        self.active: dict[str, int] = {}
        ex.on_service_call = self.incoming
        log.info("Dienste-Plugins: %s", ", ".join(sorted(self.plugins)) or "keine")

    def liste(self) -> list[dict]:
        return [{"id": pid, "name": m.DIENST.get("name", pid), "beschreibung": m.DIENST.get("beschreibung", "")}
                for pid, m in sorted(self.plugins.items())]

    async def incoming(self, entry: dict, packets: list, reader, writer, ip: str,
                       version_sent: bool = False) -> None:
        dienst = self.store.dienst(entry["number"])
        mod = self.plugins.get(dienst["plugin"]) if dienst else None
        line = telex.Line(reader, writer, server=True, answerback=(dienst or {}).get("kennung", ""))
        line._version_sent = version_sent
        result, anrufer = "beendet", ""
        try:
            if not mod:
                await line.write(P.pkt_reject("abs"))
                result = "Plugin fehlt"
                return
            line.start(preloaded=list(packets))
            await telex.first_of(line.started, line.closed, timeout=30)
            if not line.started.is_set():
                result = "abgebrochen vor Verbindungsaufbau"
                return
            await asyncio.sleep(0.8)
            ab = line.self_identification() or await line.who_are_you()
            anrufer = telex.number_from_answerback(ab)
            await line.send_own_answerback()
            key = anrufer or "unbekannt"
            speicher = self.store.daten_get(dienst["plugin"], key)
            s = Sitzung(line, anrufer, ab, speicher, dienst)
            self.active[dienst["number"]] = self.active.get(dienst["number"], 0) + 1
            try:
                await asyncio.wait_for(mod.run(s), timeout=MAX_CALL)
                result = "Dienst beendet"
            except Aufgelegt:
                result = "aufgelegt"
            except asyncio.TimeoutError:
                result = "Zeitlimit"
            except Exception:
                log.exception("Plugin %s", dienst["plugin"])
                result = "Fehler im Plugin"
            finally:
                self.active[dienst["number"]] -= 1
                if anrufer:
                    self.store.daten_set(dienst["plugin"], key, s.speicher)
            if s.verbunden:
                await line.drain_remote(timeout=30)
        except (telex.CallFailed, ConnectionError, OSError):
            result = "Verbindung abgebrochen"
        finally:
            await line.close()
            self.store.log("dienst", number=entry["number"], ip=ip,
                           detail=f"{(dienst or {}).get('plugin', '?')}: {('von ' + anrufer + ', ') if anrufer else ''}{result}")
