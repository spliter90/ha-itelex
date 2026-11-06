"""Rundsenden: ein Fernschreiben an alle Mitglieder einer Gruppe verteilen.

Eine Gruppe hat eine eigene Telexnummer auf der Vermittlung (Durchwahl wie eine Station „über
Vermittlung“). Wer sie anruft, wird mit der Kennung der Gruppe begrüßt, schreibt seinen Text und
beendet mit NNNN oder durch Auflegen. Die Vermittlung stellt den Text jedem Mitglied einzeln zu
(Wiederholungen bei besetzt / nicht erreichbar) und schickt dem Absender danach einen
Zustellbericht per Telex.

Steuerbefehle per Telex an die Gruppennummer (wenn die Gruppe es erlaubt): ANMELDEN, ABMELDEN.
Rundsendungen lassen sich auch aus der Oberfläche auslösen.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import protocol as P
import telex
from exchange import Exchange
from store import Store

log = logging.getLogger("vermittlung.rundsenden")

PARALLEL = 3                     # gleichzeitige Zustellungen
RETRY_CODES = {"occ", "nc", "der"}
RETRY_DELAYS = [90, 240]         # Sekunden bis zum 2. und 3. Versuch
IDLE_END = 90                    # Sekunden ohne Eingabe -> Text gilt als fertig
MAX_CALL = 15 * 60               # längster Anruf
MAX_TEXT = 20000
END_RE = re.compile(r"\bNNNN\b")
CODES = {"occ": "besetzt", "nc": "nicht erreichbar", "der": "Fernschreiber startet nicht",
         "abs": "abgeschaltet", "na": "nicht zugelassen", "unbekannt": "Nummer unbekannt"}


def code_text(code: str, detail: str = "") -> str:
    return CODES.get(code, detail or code)


def stamp(ts: float | None = None) -> str:
    return time.strftime("%d.%m.%y %H:%M", time.localtime(ts or time.time()))


class Rundsenden:
    def __init__(self, ex: Exchange, store: Store) -> None:
        self.ex = ex
        self.store = store
        self._sem = asyncio.Semaphore(PARALLEL)
        self._running: dict[int, asyncio.Task] = {}
        self.active_calls = 0
        ex.on_group_call = self.incoming

    def start(self) -> None:
        for rid in self.store.rs_open():
            self._spawn(rid)

    async def stop(self) -> None:
        for t in self._running.values():
            t.cancel()

    def _spawn(self, rid: int) -> None:
        if rid not in self._running:
            self._running[rid] = asyncio.create_task(self._deliver(rid))

    # --- Berechtigung --------------------------------------------------------------
    def may_send(self, gruppe: dict, sender: str) -> bool:
        if gruppe["berechtigung"] == "alle":
            return True
        if not sender:
            return False
        if gruppe["berechtigung"] == "liste":
            return sender in (gruppe["erlaubt"] or "").split(",") or self.store.ist_mitglied(gruppe["number"], sender)
        return self.store.ist_mitglied(gruppe["number"], sender)

    # --- Rundsendung anlegen ----------------------------------------------------------
    def broadcast(self, gruppe_nr: str, text: str, *, absender: str = "", kennung: str = "",
                  quelle: str = "verwaltung") -> int:
        gruppe = self.store.gruppe(gruppe_nr)
        if not gruppe:
            raise ValueError("Unbekannte Gruppe")
        text = text.strip()
        if not text:
            raise ValueError("Kein Text")
        if len(text) > MAX_TEXT:
            raise ValueError("Text zu lang")
        empfaenger = [m for m in self.store.mitglieder(gruppe_nr) if m["number"] != absender]
        if not empfaenger:
            raise ValueError("Die Gruppe hat keine Empfänger")
        rid = self.store.rs_create(gruppe_nr, text, empfaenger, absender=absender, kennung=kennung,
                                   quelle=quelle)
        self.store.log("rundsenden", number=gruppe_nr,
                       detail=f"Rundsendung {rid} von {kennung or absender or quelle} an {len(empfaenger)} Empfänger")
        log.info("Rundsendung %s an Gruppe %s: %s Empfänger", rid, gruppe_nr, len(empfaenger))
        self._spawn(rid)
        return rid

    def message_text(self, gruppe: dict, rs: dict) -> str:
        if not gruppe["kopf"]:
            return rs["text"]
        von = rs["absender_kennung"] or rs["absender"] or "VERWALTUNG"
        return (f"RUNDSENDUNG {gruppe['name']} ({gruppe['number']}) NR {rs['id']}\n"
                f"VON {von} {stamp(rs['ts'])}\n\n{rs['text']}")

    # --- Zustellen -----------------------------------------------------------------------
    async def target_for(self, number: str) -> tuple[telex.Target | None, str, str]:
        own = self.store.get(number)
        if own and own["enabled"] and own["mode"] == "vermittlung" and not self.ex.redirect_target(own):
            # Station an dieser Vermittlung: intern zustellen, auch ohne öffentliche Adresse
            ext = f"{int(own['ext_code']):02d}"
            return telex.Target("127.0.0.1", self.ex.cfg.vx_port, ext), own["name"], ""
        rec, src = await self.ex.lookup(number)
        if not rec:
            return None, "", src
        host, port = self.ex.local_target(rec)
        if not host or not port:
            return None, rec.get("name") or "", "keine Adresse"
        ext = P.decode_extension(int(rec.get("extension") or 0))
        return telex.Target(host, port, ext, rec.get("type") in (3, 4)), rec.get("name") or "", ""

    async def _deliver_one(self, rs: dict, gruppe: dict, z: dict) -> None:
        number, tries = z["number"], int(z["tries"] or 0)
        if z["next_try"] and z["next_try"] > time.time():
            await asyncio.sleep(z["next_try"] - time.time())
        while True:
            async with self._sem:
                tries += 1
                self.store.zu_update(rs["id"], number, status="offen", tries=tries, detail="wird zugestellt")
                target, name, why = await self.target_for(number)
                if target is None:
                    res = telex.Result(False, "unbekannt", why)
                else:
                    res = await telex.send(target, self.message_text(gruppe, rs), gruppe["kennung"])
            if res.ok:
                self.store.zu_update(rs["id"], number, status="zugestellt", detail=res.detail,
                                     answerback=res.answerback, name=z["name"] or name)
                return
            if res.code in RETRY_CODES and tries <= len(RETRY_DELAYS):
                wait = RETRY_DELAYS[tries - 1]
                self.store.zu_update(rs["id"], number, status="wartet", next_try=time.time() + wait,
                                     detail=f"{code_text(res.code, res.detail)}, neuer Versuch {stamp(time.time() + wait)}")
                await asyncio.sleep(wait)
                continue
            self.store.zu_update(rs["id"], number, status="fehler", detail=code_text(res.code, res.detail))
            return

    async def _deliver(self, rid: int) -> None:
        try:
            rs = self.store.rs_get(rid)
            gruppe = self.store.gruppe(rs["gruppe"]) if rs else None
            if not rs or not gruppe:
                if rs:
                    self.store.rs_update(rid, status="fertig", bericht="Gruppe gelöscht")
                return
            todo = [z for z in rs["zustellungen"] if z["status"] in ("offen", "wartet")]
            await asyncio.gather(*(self._deliver_one(rs, gruppe, z) for z in todo))
            rs = self.store.rs_get(rid)
            ok = sum(1 for z in rs["zustellungen"] if z["status"] == "zugestellt")
            self.store.log("rundsenden", number=rs["gruppe"], ok=ok == len(rs["zustellungen"]),
                           detail=f"Rundsendung {rid}: {ok} von {len(rs['zustellungen'])} zugestellt")
            bericht = await self._report(rs, gruppe) if rs["quelle"] == "telex" and rs["absender"] else ""
            self.store.rs_update(rid, status="fertig", bericht=bericht)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Rundsendung %s", rid)
            self.store.rs_update(rid, status="fertig", bericht="Fehler bei der Zustellung")
        finally:
            self._running.pop(rid, None)

    def report_text(self, rs: dict, gruppe: dict) -> str:
        zs = rs["zustellungen"]
        ok = [z for z in zs if z["status"] == "zugestellt"]
        bad = [z for z in zs if z["status"] != "zugestellt"]
        lines = [f"ZUSTELLBERICHT RUNDSENDUNG NR {rs['id']}",
                 f"GRUPPE {gruppe['name']} ({gruppe['number']}) VOM {stamp(rs['ts'])}",
                 f"ZUGESTELLT: {len(ok)} VON {len(zs)}"]
        if bad:
            lines.append("NICHT ERREICHT:")
            lines += [f"{z['number']} {z['name']} - {z['detail']}".strip() for z in bad]
        return "\n".join(lines)

    async def _report(self, rs: dict, gruppe: dict) -> str:
        target, _, why = await self.target_for(rs["absender"])
        if target is None:
            return f"Bericht nicht gesendet: Absender {why}"
        text = self.report_text(rs, gruppe)
        for attempt, wait in enumerate([0] + RETRY_DELAYS):
            if wait:
                await asyncio.sleep(wait)
            res = await telex.send(target, text, gruppe["kennung"])
            if res.ok:
                return "Bericht zugestellt"
            if res.code not in RETRY_CODES:
                break
        return f"Bericht nicht zugestellt: {code_text(res.code, res.detail)}"

    # --- Anruf an eine Gruppennummer ---------------------------------------------------------
    async def incoming(self, entry: dict, packets: list, reader, writer, ip: str,
                       version_sent: bool = False) -> None:
        gruppe = self.store.gruppe(entry["number"])
        line = telex.Line(reader, writer, server=True, answerback=gruppe["kennung"] if gruppe else "")
        line._version_sent = version_sent
        self.active_calls += 1
        result = "ohne Text beendet"
        sender = ""
        try:
            if not gruppe:
                await line.write(P.pkt_reject("abs"))
                return
            line.start(preloaded=[(t, p) for t, p in packets])
            await telex.first_of(line.started, line.closed, timeout=30)
            if not line.started.is_set():
                result = "abgebrochen vor Verbindungsaufbau"
                return
            await asyncio.sleep(0.8)
            ab = line.self_identification() or await line.who_are_you()
            sender = telex.number_from_answerback(ab)
            allowed = self.may_send(gruppe, sender)
            n = len([m for m in self.store.mitglieder(gruppe["number"]) if m["number"] != sender])
            await line.send_own_answerback()
            greet = [f"RUNDSENDEN {gruppe['name']} - {n} TEILNEHMER"]
            if allowed:
                greet.append("TEXT SENDEN, ENDE MIT NNNN")
            elif gruppe["selbst"]:
                greet.append("NUR ANMELDEN ODER ABMELDEN MOEGLICH" if sender else "KENNUNG FEHLT")
            else:
                greet.append("NICHT BERECHTIGT")
            await line.send_text("\r\n" + "\r\n".join(P.normalize(g) for g in greet) + "\r\n")
            if not allowed and not (gruppe["selbst"] and sender):
                result = "nicht berechtigt"
                await line.drain_remote(timeout=15)
                return

            # Ab Anfang auswerten: manche Stationen senden den Text direkt nach ihrer Kennung,
            # noch während unsere Begrüßung läuft. Kennungen und Wer da? filtert remote_text().
            start = 0
            began = time.monotonic()
            while not line.closed.is_set():
                await line._wait_progress(1.0)
                if END_RE.search(line.rx_text[start:]):
                    break
                if time.monotonic() - line.last_rx > IDLE_END and time.monotonic() - began > IDLE_END:
                    break
                if time.monotonic() - began > MAX_CALL:
                    break
            raw = line.remote_text(start)
            m = END_RE.search(raw)
            text = (raw[:m.start()] if m else raw).strip()
            # Steuerbefehl: eine kurze Nachricht mit einer Zeile ANMELDEN / ABMELDEN
            lines = [re.sub(r"[^A-Z]", "", ln.upper()) for ln in text.split("\n") if ln.strip()]
            word = next((w for w in lines if w in ("ANMELDEN", "ABMELDEN")), "") if len(lines) <= 3 else ""
            reply = ""
            if word in ("ANMELDEN", "ABMELDEN"):
                if not gruppe["selbst"] or not sender:
                    reply, result = "NICHT MOEGLICH", f"{word.lower()} abgelehnt"
                elif word == "ANMELDEN":
                    self.store.mitglied_add(gruppe["number"], sender, ab[:40], quelle="telex")
                    reply, result = f"{sender} ANGEMELDET", "angemeldet"
                    self.store.log("rundsenden", number=gruppe["number"], ip=ip, detail=f"{sender} per Telex angemeldet")
                else:
                    self.store.mitglied_del(gruppe["number"], sender)
                    reply, result = f"{sender} ABGEMELDET", "abgemeldet"
                    self.store.log("rundsenden", number=gruppe["number"], ip=ip, detail=f"{sender} per Telex abgemeldet")
            elif text and allowed:
                try:
                    rid = self.broadcast(gruppe["number"], text, absender=sender, kennung=ab, quelle="telex")
                    count = len(self.store.rs_get(rid)["zustellungen"])
                    reply, result = f"UEBERNOMMEN NR {rid} - WIRD AN {count} TEILNEHMER VERTEILT", f"Rundsendung {rid}"
                except ValueError as e:
                    reply, result = "NICHT UEBERNOMMEN - " + P.normalize(str(e)), f"abgelehnt: {e}"
            elif text:
                reply, result = "NICHT BERECHTIGT", "nicht berechtigt"
            if reply and not line.closed.is_set():
                await line.send_text("\r\n" + P.normalize(reply) + "\r\n")
                await line.send_own_answerback()
                await line.drain_remote(timeout=20)
        except (telex.CallFailed, ConnectionError, OSError):
            pass
        finally:
            self.active_calls -= 1
            await line.close()
            self.store.log("rundsenden", number=entry["number"], ip=ip,
                           ok=result not in ("nicht berechtigt",),
                           detail=f"Anruf {('von ' + sender + ', ') if sender else ''}{result}")
