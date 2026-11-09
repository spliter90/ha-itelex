"""i-Telex-Station: nimmt Anrufe an und sendet Nachrichten (gleichzeitig)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from dataclasses import dataclass, field

import grafiken
import protocol as P
import tns
from store import Store, is_ip, number_from_answerback

log = logging.getLogger("itelex.station")

READY_TIMEOUT = 25        # Sekunden bis die Gegenstelle ihren Fernschreiber gestartet hat
STALL_TIMEOUT = 40        # Sekunden ohne Druckfortschritt -> Abbruch
IDLE_TIMEOUT = 300        # Sekunden ohne Daten bei eingehender Verbindung
MAX_UNPRINTED = 8         # Flusskontrolle: so viele Zeichen dürfen ungedruckt unterwegs sein
CHUNK = 10                # Zeichen pro Baudot-Paket beim Senden
RETRY_REASONS = {"occ", "nc", "der"}
RETRY_DELAYS = [90, 240]  # Wiederholungen bei besetzt / nicht erreichbar
LINGER_CHOICES = (0, 30, 60, 120, 300)  # Leitung nach dem Senden offen halten (Sekunden)
LINGER_DEFAULT = 60
TYPING_WINDOW = 3         # Sekunden: so lange gilt die Gegenstelle als "schreibt"
_ids = itertools.count(1)


@dataclass
class Config:
    number: str = ""
    answerback: str = ""
    port: int = 134
    tns_servers: list[str] = field(default_factory=lambda: [
        "tlnserv.teleprinter.net", "tlnserv2.teleprinter.net", "tlnserv3.teleprinter.net"])
    tns_port: int = 11811
    identify_caller: bool = True
    confirm_delivery: bool = True
    retention_days: int = 90
    linger: int = 300             # Sekunden Ruhe nach dem Senden bis zum Auflegen
    idle_in: int = 300            # Sekunden Ruhe bei eingehenden Anrufen bis zum Auflegen
    tns_dynip: bool = False       # eigene IP stündlich beim TNS melden
    tns_pin: int = 0
    public_port: int = 134
    vx_host: str = ""             # eigene/öffentliche Vermittlung (Centralex), leer = aus
    vx_port: int = 49491


RETENTION_CHOICES = (30, 60, 90, 180, 365)
AB_IDLE_CHOICES = (30, 60, 120, 180, 300)
AB_DEFAULTS = {
    "ab_on": "0",
    "ab_text": "{kennung}: zur zeit nicht besetzt. bitte schreiben sie ihre nachricht, "
               "sie wird gespeichert.",
    "ab_end": "danke, ihre nachricht ist gespeichert. mfg",
    "ab_idle": "60",
    "ab_until": "",
}


def format_until(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%d.%m. %H:%M", time.localtime(ts))

GREETING_DEFAULTS = {
    "greet_start": "{datum} {zeit}",
    "greet_end": "mfg",
    "greet_in": "",
}
PLACEHOLDERS = ("{datum}", "{zeit}", "{name}", "{nummer}", "{kennung}")


_ART_RE = re.compile(r"\{grafik:([^}]{1,40})\}", re.IGNORECASE)


def expand_art(text: str, store=None) -> str:
    """{grafik:herz} durch die Grafik ersetzen, jeweils auf eigenen Zeilen."""
    def repl(m: re.Match) -> str:
        art = grafiken.lookup(m.group(1), store)
        return f"\n{art}\n" if art else m.group(0)
    out = _ART_RE.sub(repl, text)
    return re.sub(r"\n{3,}", "\n\n", out) if out != text else out


def render_greeting(template: str, *, name: str = "", number: str = "", own: str = "",
                    zurueck: str = "") -> str:
    """Platzhalter ersetzen; Ergebnis ist fernschreibfertig (Großschrift, umbrochen)."""
    if not template.strip():
        return ""
    now = time.localtime()
    values = {"datum": time.strftime("%d.%m.%y", now), "zeit": time.strftime("%H:%M", now),
              "name": name, "nummer": number, "kennung": own, "zurueck": zurueck}
    text = re.sub(r"\{(datum|zeit|name|nummer|kennung|zurueck)\}", lambda m: values[m.group(1).lower()],
                  template, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return P.wrap(P.normalize(text)) if text else ""


class CallFailed(Exception):
    def __init__(self, code: str, text: str | None = None):
        self.code = code
        super().__init__(text or P.REJECT_REASONS.get(code, code))


async def first_of(*events: asyncio.Event, timeout: float) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


def clean_answerback(text: str) -> str:
    text = text.replace(P.WRU, " ").replace(P.BELL, " ")
    return re.sub(r"\s+", " ", text).strip()


class Connection:
    """Eine i-Telex-Verbindung (Binärprotokoll) in beide Richtungen."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 server: bool, station: "Station") -> None:
        self.reader, self.writer, self.server, self.station = reader, writer, server, station
        self.enc = P.BaudotEncoder()
        self.dec = P.BaudotDecoder()
        self.sent = 0
        self.remote_printed: int | None = None
        self.received = 0
        self.rx_text = ""
        self.last_rx = time.monotonic()
        self.started = asyncio.Event()      # wir (Server) haben "Fernschreiber gestartet"
        self.ready = asyncio.Event()        # Gegenstelle ist empfangsbereit (Ack/Baudot)
        self.progress = asyncio.Event()     # neues Ack oder neue Daten
        self.closed = asyncio.Event()
        self.reject: str | None = None
        self.remote_version: int | None = None
        self._version_sent = False
        self._wlock = asyncio.Lock()
        self.answerback_spans: list[tuple[int, int]] = []
        self.answerbacks: set[str] = set()
        self._tlock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # Sitzung (live sichtbar im Chat)
        self.id = next(_ids)
        self.direction = "in" if server else "out"
        self.peer = ""
        self.peer_name = ""
        self.remote_ab = ""
        self.ip = ""
        self.started_at = time.time()
        self.last_tx = time.monotonic()
        self.capturing = 0
        self.seg_start = 0            # Beginn des aktuell eingehenden Textabschnitts in rx_text
        self.cur_in_mid: int | None = None
        self.in_mids: list[int] = []
        self.flush_task: asyncio.Task | None = None
        self.hangup = asyncio.Event()  # Auflegen auf Wunsch des Benutzers
        self.idle_limit = IDLE_TIMEOUT
        self.via_ab = False             # Anruf wurde vom Anrufbeantworter angenommen

    @property
    def last_activity(self) -> float:
        return max(self.last_rx, self.last_tx)

    # --- Senden --------------------------------------------------------------
    async def write(self, data: bytes) -> None:
        if self.closed.is_set():
            raise CallFailed("nc", "Verbindung getrennt")
        if log.isEnabledFor(logging.DEBUG) and data[:1] != bytes([P.ACK]):
            log.debug("[%s] >> %s", self.id, P.describe(data[0], data[2:]))
        async with self._wlock:
            self.writer.write(data)
            await self.writer.drain()

    async def send_version(self) -> None:
        self._version_sent = True
        await self.write(P.pkt_version())

    def unprinted(self) -> int:
        if self.remote_printed is None:
            return 0
        return (self.sent - self.remote_printed) & 0xFF

    async def _wait_progress(self, timeout: float) -> bool:
        self.progress.clear()
        try:
            await asyncio.wait_for(self.progress.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def send_baudot(self, data: bytes) -> None:
        i = 0
        stalled_since = time.monotonic()
        last_unprinted = self.unprinted()
        while i < len(data):
            if self.closed.is_set():
                raise CallFailed("nc", "Gegenstelle hat aufgelegt")
            up = self.unprinted()
            if up < MAX_UNPRINTED:
                chunk = data[i:i + min(CHUNK, MAX_UNPRINTED - up + 2)]
                await self.write(P.pkt(P.BAUDOT, chunk))
                self.sent += len(chunk)
                self.last_tx = time.monotonic()
                i += len(chunk)
                stalled_since = time.monotonic()
                continue
            await self._wait_progress(1.0)
            if self.unprinted() < last_unprinted:
                stalled_since = time.monotonic()
            last_unprinted = self.unprinted()
            if time.monotonic() - stalled_since > STALL_TIMEOUT:
                raise CallFailed("der", "Gegenstelle druckt nicht mehr")

    async def send_text(self, text: str) -> None:
        async with self._tlock:  # Antwort auf "Wer da?" nie mitten in einen Text mischen
            await self.send_baudot(self.enc.encode(text))

    async def drain_remote(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.unprinted() and time.monotonic() < deadline and not self.closed.is_set():
            await self._wait_progress(1.0)
        return self.unprinted() == 0

    async def who_are_you(self, wait: float = 8.0) -> str:
        """Wer da? senden und die Kennung der Gegenstelle einsammeln."""
        self.capturing += 1
        try:
            return await self._who_are_you(wait)
        finally:
            self.capturing -= 1

    async def _who_are_you(self, wait: float) -> str:
        await self.send_text(P.WRU)
        start = len(self.rx_text)
        end = start
        deadline = time.monotonic() + wait
        a = start
        found = False
        while time.monotonic() < deadline and not self.closed.is_set():
            await self._wait_progress(0.4)
            seg = self.rx_text[start:]
            # Kennung = erste vollständige Zeile nach dem "Wer da?", die eine Nummer enthält
            for m in re.finditer(r"[^\s\x05\x07][^\r\n]*[\r\n]", seg):
                line = clean_answerback(m.group(0))
                if len(line) <= 30 and number_from_answerback(line):
                    a, end, found = start + m.start(), start + m.end(), True
                    break
            if found:
                break
            visible = re.sub(r"[\s\x05\x07]", "", seg)
            if visible and time.monotonic() - self.last_rx > 2.5:
                break
        if not found:
            # Ersatz: erste Zeile mit Inhalt (Kennung ohne Nummer)
            m = re.search(r"[^\s\x05\x07][^\r\n]*(?:[\r\n]|$)", self.rx_text[start:])
            if m and len(clean_answerback(m.group(0))) <= 24:
                a, end = start + m.start(), start + m.end()
        if end > a:
            self.answerback_spans.append((a, end))
        ab = clean_answerback(self.rx_text[a:end])
        if ab:
            self.answerbacks.add(ab)
        return ab

    def self_identification(self) -> str:
        """Kennung, die der Anrufer unaufgefordert als erste Zeile gesendet hat."""
        m = re.search(r"[^\s\x05\x07][^\r\n]*[\r\n]", self.rx_text)
        if not m:
            return ""
        line = clean_answerback(m.group(0))
        if len(line) > 24 or not number_from_answerback(line):
            return ""
        self.answerback_spans.append((m.start(), m.end()))
        self.answerbacks.add(line)
        return line

    async def send_own_answerback(self) -> None:
        ab = self.station.cfg.answerback
        if ab:
            await self.send_text("\r\n" + P.normalize(ab) + "\r\n")

    def remote_text(self, start: int = 0) -> str:
        """Empfangener Text ab Position start, ohne eingesammelte Kennungen."""
        out, pos = [], start
        for a, b in sorted(self.answerback_spans):
            if b <= pos:
                continue
            out.append(self.rx_text[pos:max(a, pos)])
            pos = max(pos, b)
        out.append(self.rx_text[pos:])
        text = P.to_display("".join(out).replace(P.WRU, ""))
        # Wiederholte Kennungen (z. B. selbst gesendet) aus dem Text entfernen
        lines = [ln for ln in text.split("\n") if clean_answerback(ln) not in self.answerbacks]
        return P.to_display("\n".join(lines))

    # --- Empfangen -----------------------------------------------------------
    def start(self, first_byte: bytes | None = None) -> None:
        self._task = asyncio.create_task(self._reader_loop(first_byte))
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Wie i-Telex/piTelex: bei laufender Verbindung jede Sekunde eine Bestätigung senden.
        Hält die Leitung (und NAT/Firewall) offen und meldet der Gegenseite unseren Druckstand."""
        try:
            while not self.closed.is_set():
                await asyncio.sleep(1.0)
                running = self.started.is_set() if self.server else self.ready.is_set()
                if running and not self.closed.is_set():
                    await self.write(P.pkt_ack(self.received))
        except (CallFailed, ConnectionError, OSError):
            pass

    async def _start_printer(self) -> None:
        if not self.started.is_set():
            self.started.set()
            await self.write(P.pkt_ack(self.received))

    async def _reader_loop(self, first_byte: bytes | None) -> None:
        try:
            while True:
                if first_byte is not None:
                    ln = await self.reader.readexactly(1)
                    ptype, plen = first_byte[0], ln[0]
                    payload = await self.reader.readexactly(plen) if plen else b""
                    first_byte = None
                else:
                    ptype, payload = await P.read_packet(self.reader)
                if log.isEnabledFor(logging.DEBUG) and ptype != P.ACK:
                    log.debug("[%s] << %s", self.id, P.describe(ptype, payload))
                await self._handle(ptype, payload)
                if self.closed.is_set():
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except CallFailed:
            pass
        except Exception:
            log.exception("Fehler in der Verbindung")
        finally:
            self.closed.set()
            self.progress.set()

    async def _handle(self, ptype: int, payload: bytes) -> None:
        if ptype == P.HEARTBEAT:
            return
        if ptype == P.DIRECT_DIAL and self.server:
            ext = P.decode_extension(payload[0]) if payload else None
            if ext not in (None, "0", "00"):
                await self.write(P.pkt_reject("na"))
                self.closed.set()
                return
            await self._start_printer()
        elif ptype == P.BAUDOT:
            text = self.dec.decode(payload)
            self.received += len(payload)
            self.rx_text += text
            self.last_rx = time.monotonic()
            self.ready.set()
            if self.server and not self.started.is_set():
                await self._start_printer()
            # Ohne echten Drucker gilt alles sofort als gedruckt
            await self.write(P.pkt_ack(self.received))
            if P.WRU in text:
                asyncio.create_task(self._answer_wru())
            self.progress.set()
            self.station.rx_activity(self)
        elif ptype == P.END:
            self.closed.set()
        elif ptype == P.REJECT:
            self.reject = payload.decode("ascii", "ignore").rstrip("\x00").strip() or "abs"
            self.closed.set()
        elif ptype == P.ACK and payload:
            self.remote_printed = payload[0]
            self.ready.set()
            if self.server and not self.started.is_set():
                await self._start_printer()
            self.progress.set()
        elif ptype == P.VERSION and payload:
            first = self.remote_version is None
            self.remote_version = payload[0]
            if payload[0] != P.PROTOCOL_VERSION and not first:
                await self.write(P.pkt_reject("ver"))
                self.closed.set()
            elif self.server and first and not self._version_sent:
                await self.send_version()

    async def _answer_wru(self) -> None:
        try:
            await asyncio.sleep(0.3)
            await self.send_own_answerback()
        except Exception:
            pass

    async def close(self, send_end: bool = True) -> None:
        if send_end and not self.closed.is_set():
            try:
                await self.write(P.pkt(P.END))
            except Exception:
                pass
        self.closed.set()
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 3)
        except Exception:
            pass
        if self._task:
            self._task.cancel()
        if getattr(self, "_keepalive", None):
            self._keepalive.cancel()


class Station:
    def __init__(self, cfg: Config, store: Store, on_change) -> None:
        self.cfg = cfg
        self.store = store
        self.on_change = on_change            # async callback(event: str, data: dict)
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.active = 0
        self._server: asyncio.base_events.Server | None = None
        self.connections: set[Connection] = set()
        self.sessions: dict[int, Connection] = {}
        self._dialing: set[str] = set()
        self.tns_status: dict = {"enabled": cfg.tns_dynip}
        self.vx_status: dict = {"enabled": bool(cfg.vx_host), "host": cfg.vx_host}

    @property
    def accepting(self) -> bool:
        return self.store.get("accept_calls", "1") == "1"

    def set_accepting(self, value: bool) -> None:
        self.store.put("accept_calls", "1" if value else "0")

    @property
    def retention_days(self) -> int:
        try:
            days = int(self.store.get("retention_days", str(self.cfg.retention_days)))
        except ValueError:
            days = 90
        return days if days in RETENTION_CHOICES else 90

    def set_retention(self, days: int) -> int:
        if int(days) not in RETENTION_CHOICES:
            raise ValueError("Aufbewahrung muss 30, 60, 90, 180 oder 365 Tage sein")
        self.store.put("retention_days", str(int(days)))
        return self.store.purge(int(days))

    def greetings(self) -> dict:
        return {k: self.store.get(k, v) for k, v in GREETING_DEFAULTS.items()}

    def set_greetings(self, data: dict) -> None:
        for k in GREETING_DEFAULTS:
            if k in data:
                self.store.put(k, str(data[k] or "")[:200])

    def greeting(self, key: str, peer: str, template: str | None = None, zurueck: str = "") -> str:
        """Grußzeile rendern; template=None nimmt den Standard aus den Einstellungen."""
        tpl = self.greetings()[key] if template is None else template
        return render_greeting(tpl, name=self.store.contact_name(peer),
                               number=peer if peer.isdigit() else "", own=self.cfg.answerback,
                               zurueck=zurueck)

    # --- Anrufbeantworter ----------------------------------------------------
    def answering_machine(self) -> dict:
        g = {k: self.store.get(k, v) for k, v in AB_DEFAULTS.items()}
        try:
            until = float(g["ab_until"]) if g["ab_until"] else None
        except ValueError:
            until = None
        on = g["ab_on"] == "1"
        if on and until and time.time() >= until:
            # Rückkehrzeit erreicht: automatisch ausschalten
            self.store.put("ab_on", "0")
            self.store.put("ab_until", "")
            log.info("Anrufbeantworter automatisch ausgeschaltet")
            on, until = False, None
        try:
            idle = int(g["ab_idle"])
        except ValueError:
            idle = 60
        return {"on": on, "text": g["ab_text"], "end": g["ab_end"],
                "idle": idle if idle in AB_IDLE_CHOICES else 60, "until": until,
                "until_text": format_until(until), "idle_choices": list(AB_IDLE_CHOICES)}

    def set_answering_machine(self, data: dict) -> dict:
        if "on" in data:
            v = data["on"]
            on = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "on", "an", "ein", "ja")
            self.store.put("ab_on", "1" if on else "0")
        if "text" in data:
            self.store.put("ab_text", str(data["text"] or "")[:400])
        if "end" in data:
            self.store.put("ab_end", str(data["end"] or "")[:200])
        if "idle" in data:
            if int(data["idle"]) not in AB_IDLE_CHOICES:
                raise ValueError("Ungültige Wartezeit")
            self.store.put("ab_idle", str(int(data["idle"])))
        if "until" in data:
            until = data["until"]
            if until in (None, "", 0):
                self.store.put("ab_until", "")
            else:
                ts = float(until)
                if ts <= time.time():
                    raise ValueError("Der Zeitpunkt zum Ausschalten liegt in der Vergangenheit")
                self.store.put("ab_until", str(int(ts)))
        return self.answering_machine()

    async def _ab_watch(self) -> None:
        """Jede Minute: automatisches Ausschalten prüfen und den Zustand an Home Assistant melden."""
        last = None
        while True:
            await asyncio.sleep(60)
            on = self.answering_machine()["on"]
            await self.on_change("settings" if on != last else "tick", {})
            last = on

    # --- Start ---------------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._incoming, "0.0.0.0", self.cfg.port)
        log.info("Höre auf i-Telex-Port %s", self.cfg.port)
        for m in self.store._q("SELECT id, status FROM messages WHERE direction='out'"
                               " AND status IN ('queued','sending','retry')"):
            if m["status"] == "sending":
                self.store.update_message(m["id"], status="failed", detail="Unterbrochen (Neustart)")
            else:
                self.queue.put_nowait(m["id"])
        asyncio.create_task(self._sender_worker())
        asyncio.create_task(self._ab_watch())
        if self.cfg.vx_host:
            if not (self.cfg.number.isdigit() and self.cfg.tns_pin):
                log.warning("Vermittlung eingetragen, aber Nummer oder PIN fehlt")
                self.vx_status.update(ok=False, error="Nummer oder PIN fehlt")
            else:
                self._vx_task = asyncio.create_task(self._vermittlung_loop())

    async def stop(self) -> None:
        if getattr(self, "_vx_task", None):
            self._vx_task.cancel()
        if self._server:
            self._server.close()
        for c in list(self.connections):
            await c.close()

    # --- Sitzungen (offene Leitungen) -----------------------------------------
    @property
    def linger(self) -> int:
        return max(0, int(self.cfg.linger))

    def set_linger(self, seconds: int) -> None:  # nur für Tests; sonst Add-on-Konfiguration
        self.cfg.linger = int(seconds)

    def session_for(self, peer: str) -> Connection | None:
        for c in sorted(self.sessions.values(), key=lambda c: -c.started_at):
            if c.peer == peer and not c.closed.is_set() and not c.hangup.is_set():
                return c
        return None

    def session_info(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for c in self.sessions.values():
            if c.closed.is_set():
                continue
            idle = now - c.last_activity
            out.append({
                "id": c.id, "peer": c.peer, "name": self.store.contact_name(c.peer) or c.peer_name,
                "answerback": c.remote_ab, "direction": c.direction, "since": c.started_at,
                # nur echter Text zählt, nicht die automatisch gesendete Kennung
                "typing": (now - c.last_rx) < TYPING_WINDOW
                          and bool(re.search(r"[A-Z0-9]", c.remote_text(c.seg_start))),
                "remaining": max(0, int(c.idle_limit - idle)),
            })
        return out

    def register(self, conn: Connection) -> None:
        self.sessions[conn.id] = conn
        self.rx_activity(conn)

    def rx_activity(self, conn: Connection) -> None:
        """Eingehende Zeichen zeitnah in den Chat schreiben (gebündelt)."""
        if conn.id not in self.sessions:
            return
        if conn.flush_task is None or conn.flush_task.done():
            conn.flush_task = asyncio.create_task(self._flush_later(conn))

    async def _flush_later(self, conn: Connection) -> None:
        await asyncio.sleep(0.6)
        while conn.capturing and not conn.closed.is_set():
            await asyncio.sleep(0.4)   # Kennung wird gerade eingesammelt
        await self.flush(conn)

    async def flush(self, conn: Connection, final: bool = False) -> None:
        text = conn.remote_text(conn.seg_start)
        has_text = bool(re.search(r"[A-Z0-9]", text))
        if conn.cur_in_mid is None:
            if has_text:
                conn.cur_in_mid = self.store.add_message(
                    "in", conn.peer or "unbekannt", text, peer_name=conn.peer_name,
                    answerback=conn.remote_ab, status="received" if final else "live",
                    ab=conn.via_ab)
                conn.in_mids.append(conn.cur_in_mid)
                await self.on_change("live", {})
        else:
            if has_text:
                self.store.update_message(conn.cur_in_mid, text=text,
                                          status="received" if final else "live")
            elif final:
                self.store.delete_message(conn.cur_in_mid)
                conn.in_mids.remove(conn.cur_in_mid)
                conn.cur_in_mid = None
        if final:
            if conn.cur_in_mid is not None and has_text:
                await self.on_change("received", {
                    "peer": conn.peer or "unbekannt", "text": text, "answerback": conn.remote_ab,
                    "name": self.store.contact_name(conn.peer) or conn.peer_name,
                    "anrufbeantworter": conn.via_ab})
            conn.cur_in_mid = None
            conn.seg_start = len(conn.rx_text)

    async def _hold_open(self, conn: Connection) -> str:
        """Leitung offen halten, bis aufgelegt wird oder nichts mehr passiert."""
        while not conn.closed.is_set() and not conn.hangup.is_set():
            try:
                await first_of(conn.closed, conn.hangup, timeout=2)
            except Exception:
                pass
            if conn.capturing:
                continue
            if time.monotonic() - conn.last_activity > conn.idle_limit:
                return "timeout"
        return "hangup" if conn.hangup.is_set() and not conn.closed.is_set() else "remote"

    async def _finish(self, conn: Connection, *, result: str, started: float) -> None:
        if conn.flush_task and not conn.flush_task.done():
            conn.flush_task.cancel()
        await self.flush(conn, final=True)
        await conn.close(send_end=True)
        self.sessions.pop(conn.id, None)
        self.connections.discard(conn)
        self.active -= 1
        self.store.add_call(conn.direction, peer=conn.peer if conn.peer != "unbekannt" else "",
                            peer_name=conn.peer_name, ip=conn.ip, answerback=conn.remote_ab,
                            result=result, duration=time.time() - started)
        await self.on_change("call", {})

    def extend_session(self, sid: int) -> bool:
        """Wartezeit zurücksetzen ("Länger warten")."""
        conn = self.sessions.get(sid)
        if not conn or conn.closed.is_set():
            return False
        conn.last_tx = time.monotonic()
        return True

    def end_session(self, sid: int) -> bool:
        conn = self.sessions.get(sid)
        if not conn:
            return False
        conn.hangup.set()
        return True

    async def session_wru(self, sid: int) -> str:
        conn = self.sessions.get(sid)
        if not conn or conn.closed.is_set():
            raise ValueError("Keine offene Verbindung")
        ab = await conn.who_are_you()
        if ab:
            conn.remote_ab = ab
        return ab

    KEYS = {"zl": "\n", "wr": "\r", "wrzl": "\r\n", "klingel": P.BELL}

    async def send_keys(self, sid: int, key: str) -> None:
        """Einzelne Fernschreibtasten in eine offene Leitung senden (ZL, WR, WR ZL, Klingel)."""
        conn = self.sessions.get(sid)
        if not conn or conn.closed.is_set():
            raise ValueError("Keine offene Verbindung")
        if key not in self.KEYS:
            raise ValueError("Unbekannte Taste")
        await self.flush(conn, final=True)
        await conn.send_text(self.KEYS[key])

    def send_live(self, conn: Connection, text: str, start_line: str | None = None,
                  end_line: str | None = None) -> int:
        """Text direkt in eine offene Verbindung schreiben."""
        text = text.replace("\r\n", "\n").strip("\n").rstrip()
        if not text.strip():
            raise ValueError("Leere Nachricht")
        clip = lambda v: None if v is None else str(v).strip()[:200]
        mid = self.store.add_message("out", conn.peer or "unbekannt", text, status="sending",
                                     detail="Wird in die offene Verbindung geschrieben", read=True,
                                     start_line="", end_line="")
        asyncio.create_task(self._live_write(conn, mid))
        return mid

    async def _live_write(self, conn: Connection, mid: int) -> None:
        msg = self.store.message(mid)
        await self.flush(conn, final=True)   # bisherige Antwort abschließen, Reihenfolge im Chat
        # Offene Leitung: genau das senden, was getippt wurde. Keine Anfangs-/Endzeile, sonst
        # werten Menüdienste Datum oder „mfg“ als Eingabe. Am Ende WR ZL zum Bestätigen.
        payload = P.wrap(P.normalize(expand_art(msg["text"], self.store))) + "\r\n"
        try:
            await conn.send_text(payload)
            await self.on_change("status", {"id": mid})
            printed = await conn.drain_remote(timeout=30 + len(payload) * 0.2)
            self.store.update_message(mid, status="delivered" if printed else "sent",
                                      detail="Gedruckt" if printed else "Übertragen")
        except CallFailed as e:
            self.store.update_message(mid, status="failed", detail=str(e))
        await self.on_change("sent", {"id": mid})

    # --- Senden --------------------------------------------------------------
    def enqueue(self, to: str, text: str, greet: bool = True, start_line: str | None = None,
                end_line: str | None = None) -> int:
        text = text.replace("\r\n", "\n").strip("\n").rstrip()  # Einrückung (Grafiken) behalten
        if not text.strip():
            raise ValueError("Leere Nachricht")
        to = to.strip().replace(" ", "")
        live = self.session_for(to)
        if live:
            return self.send_live(live, text, start_line, end_line)
        if not (to.isdigit() or ":" in to):
            raise ValueError("Ziel muss eine Telexnummer sein")
        clip = lambda v: None if v is None else str(v).strip()[:200]
        mid = self.store.add_message("out", to, text, status="queued", read=True, greet=greet,
                                     start_line=clip(start_line), end_line=clip(end_line))
        self.queue.put_nowait(mid)
        asyncio.create_task(self.on_change("queued", {"id": mid}))
        return mid

    async def _sender_worker(self) -> None:
        while True:
            mid = await self.queue.get()
            try:
                await self._send_with_retry(mid)
            except Exception:
                log.exception("Senden fehlgeschlagen")
                self.store.update_message(mid, status="failed", detail="Interner Fehler")
            await self.on_change("sent", {"id": mid})

    async def _send_with_retry(self, mid: int) -> None:
        attempt = 0
        while True:
            try:
                await self._send(mid)
                return
            except CallFailed as e:
                if e.code in RETRY_REASONS and attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt]
                    attempt += 1
                    self.store.update_message(mid, status="retry",
                                              detail=f"{e} – neuer Versuch in {delay // 60} min")
                    await self.on_change("status", {"id": mid})
                    await asyncio.sleep(delay)
                    continue
                self.store.update_message(mid, status="failed", detail=str(e))
                return

    async def _resolve(self, to: str) -> tns.Peer:
        if ":" in to:  # direkte Adresse host:port (Tests, lokale Stationen)
            host, port = to.rsplit(":", 1)
            return tns.Peer(to, "", host, int(port), None, False)
        try:
            peer = await tns.lookup(to, self.cfg.tns_servers, self.cfg.tns_port)
        except tns.TNSError as e:
            raise CallFailed("tns", str(e)) from e
        if peer is None:
            raise CallFailed("unknown", "Nummer nicht im Teilnehmerverzeichnis")
        return peer

    async def _send(self, mid: int) -> None:
        msg = self.store.message(mid)
        if not msg:
            return
        live = self.session_for(msg["peer"])
        if live:  # Leitung zu dieser Gegenstelle ist schon offen
            self.store.update_message(mid, status="sending", detail="Wird in die offene Verbindung geschrieben")
            await self._live_write(live, mid)
            return
        self.store.update_message(mid, status="sending", detail="Verbindung wird aufgebaut")
        await self.on_change("status", {"id": mid})
        peer = await self._resolve(msg["peer"])
        if peer.name:
            self.store.update_message(mid, peer_name=peer.name)
        started = time.time()
        body = P.wrap(P.normalize(expand_art(msg["text"], self.store)))

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(peer.host, peer.port), timeout=10)
        except (OSError, asyncio.TimeoutError) as e:
            self.store.add_call("out", peer=msg["peer"], peer_name=peer.name, result="nicht erreichbar")
            raise CallFailed("nc", "Gegenstelle nicht erreichbar") from e

        if peer.ascii:
            await self._send_ascii(mid, msg, peer, reader, writer, body, started)
            return

        conn = Connection(reader, writer, server=False, station=self)
        conn.peer, conn.peer_name, conn.ip = msg["peer"], peer.name, peer.host
        self.connections.add(conn)
        self.active += 1
        await self.on_change("call", {})
        result, handed_off = "abgebrochen", False
        try:
            conn.start()
            await conn.send_version()
            await conn.write(P.pkt_direct_dial(peer.extension))
            await first_of(conn.ready, conn.closed, timeout=READY_TIMEOUT)
            if conn.reject:
                result = P.REJECT_REASONS.get(conn.reject, conn.reject)
                raise CallFailed(conn.reject)
            if not conn.ready.is_set():
                result = "Fernschreiber startet nicht"
                raise CallFailed("der")

            self.register(conn)  # ab jetzt erscheint Eingehendes live im Chat
            self.store.update_message(mid, detail="Verbunden, Text wird übertragen")
            await self.on_change("status", {"id": mid})
            await conn.send_baudot(conn.enc.force_letters())
            conn.remote_ab = await conn.who_are_you()
            if conn.remote_ab:
                self.store.update_message(mid, answerback=conn.remote_ab)
            await conn.send_own_answerback()
            greet = bool(msg.get("greet", 1))
            start_line = self.greeting("greet_start", msg["peer"], msg.get("start_line")) if greet else ""
            end_line = self.greeting("greet_end", msg["peer"], msg.get("end_line")) if greet else ""
            if start_line:
                await conn.send_text("\r\n" + start_line + "\r\n")
            await conn.send_text("\r\n" + body + "\r\n\r\n")
            if end_line:
                await conn.send_text(end_line + "\r\n")
            delivered_ab = ""
            if self.cfg.confirm_delivery:
                delivered_ab = await conn.who_are_you(wait=10)
            await conn.drain_remote(timeout=30 + len(body) * 0.2)
            if conn.reject:
                raise CallFailed(conn.reject)
            status = "delivered" if delivered_ab else "sent"
            detail = ("Zugestellt, Kennung " + delivered_ab) if delivered_ab else "Übertragen (ohne Bestätigung)"
            self.store.update_message(mid, status=status, detail=detail,
                                      answerback=delivered_ab or conn.remote_ab)
            result = "zugestellt" if delivered_ab else "übertragen"
            if self.linger and not conn.closed.is_set():
                conn.idle_limit = self.linger
                handed_off = True
                asyncio.create_task(self._linger_out(conn, result, started))
        except CallFailed:
            if result == "abgebrochen" and conn.closed.is_set():
                result = "Gegenstelle hat aufgelegt"
            raise
        finally:
            if not handed_off:
                await self._finish(conn, result=result, started=started)

    async def _linger_out(self, conn: Connection, result: str, started: float) -> None:
        """Nach dem Senden auf eine Antwort warten; eigene Antworten gehen in dieselbe Leitung."""
        await self.on_change("call", {})
        how = await self._hold_open(conn)
        if conn.in_mids or conn.cur_in_mid is not None or conn.remote_text(conn.seg_start):
            result += ", Antwort erhalten"
        if how == "hangup":
            result += ", aufgelegt"
        await self._finish(conn, result=result, started=started)

    async def dial(self, to: str) -> dict:
        """Gegenstelle anrufen, ohne Text zu senden: Leitung aufbauen, Kennungen austauschen und
        offen halten. Danach wird live geschrieben, wie am Fernschreiber."""
        to = to.strip().replace(" ", "")
        if not (to.isdigit() or ":" in to):
            raise ValueError("Bitte eine Telexnummer angeben")
        live = self.session_for(to)
        if live:
            return {"peer": to, "answerback": live.remote_ab, "schon_offen": True}
        if to in self._dialing:
            raise ValueError("Die Nummer wird gerade angerufen")
        self._dialing.add(to)
        try:
            return await self._dial(to)
        finally:
            self._dialing.discard(to)

    async def _dial(self, to: str) -> dict:
        peer = await self._resolve(to)
        if peer.ascii:
            raise CallFailed("na", "ASCII-Station: bitte direkt eine Nachricht senden")
        started = time.time()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(peer.host, peer.port), timeout=10)
        except (OSError, asyncio.TimeoutError) as e:
            self.store.add_call("out", peer=to, peer_name=peer.name, result="nicht erreichbar")
            raise CallFailed("nc", "Gegenstelle nicht erreichbar") from e
        conn = Connection(reader, writer, server=False, station=self)
        conn.peer, conn.peer_name, conn.ip = to, peer.name, peer.host
        self.connections.add(conn)
        self.active += 1
        await self.on_change("call", {})
        result, handed_off = "abgebrochen", False
        try:
            conn.start()
            await conn.send_version()
            await conn.write(P.pkt_direct_dial(peer.extension))
            await first_of(conn.ready, conn.closed, timeout=READY_TIMEOUT)
            if conn.reject:
                result = P.REJECT_REASONS.get(conn.reject, conn.reject)
                raise CallFailed(conn.reject)
            if not conn.ready.is_set():
                result = "Fernschreiber startet nicht"
                raise CallFailed("der")
            self.register(conn)
            await conn.send_baudot(conn.enc.force_letters())
            conn.remote_ab = await conn.who_are_you()
            await conn.send_own_answerback()
            conn.idle_limit = self.linger or 300
            result = "Anruf ohne Text"
            handed_off = True
            asyncio.create_task(self._linger_out(conn, result, started))
            return {"peer": to, "answerback": conn.remote_ab,
                    "name": self.store.contact_name(to) or peer.name}
        except CallFailed:
            if result == "abgebrochen" and conn.closed.is_set():
                result = "Gegenstelle hat aufgelegt"
            raise
        finally:
            if not handed_off:
                await self._finish(conn, result=result, started=started)

    async def _send_ascii(self, mid, msg, peer, reader, writer, body, started) -> None:
        try:
            writer.write(("\r\n" + P.normalize(self.cfg.answerback) + "\r\n" + body + "\r\n")
                         .encode("ascii", "replace"))
            await writer.drain()
            await asyncio.sleep(3)
        finally:
            writer.close()
        self.store.update_message(mid, status="sent", detail="Übertragen (ASCII-Station)")
        self.store.add_call("out", peer=msg["peer"], peer_name=peer.name, ip=peer.host,
                            result="übertragen (ASCII)", duration=time.time() - started)

    # --- Empfangen -----------------------------------------------------------
    async def _incoming(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        via: str | None = None) -> None:
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if via:
            ip = via   # Anruf kommt über die Vermittlung, echte Adresse des Anrufers ist unbekannt
        started = time.time()
        log.info("Eingehende Verbindung von %s", ip)

        async def refuse(reason: str, result: str) -> None:
            try:
                writer.write(P.pkt_reject(reason))
                await writer.drain()
            except Exception:
                pass
            writer.close()
            self.store.add_call("in", ip=ip, result=result)
            await self.on_change("call", {})

        if not self.accepting:
            await refuse("abs", "abgewiesen (Annahme aus)")
            return
        if not via and self.store.is_ip_blocked(ip):
            await refuse("occ", "gesperrt (IP)")
            return

        try:
            first = await asyncio.wait_for(reader.readexactly(1), timeout=30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            self.store.add_call("in", ip=ip, result="keine Daten")
            return

        if first[0] not in P.KNOWN_TYPES:
            await self._incoming_ascii(first, reader, writer, ip, started)
            return

        conn = Connection(reader, writer, server=True, station=self)
        conn.ip, conn.peer = ip, "unbekannt"
        self.connections.add(conn)
        self.active += 1
        await self.on_change("call", {})
        result = "angenommen"
        try:
            conn.start(first_byte=first)
            await first_of(conn.started, conn.closed, timeout=30)
            if not conn.started.is_set():
                result = "abgebrochen vor Verbindungsaufbau"
                return

            await asyncio.sleep(0.8)
            conn.remote_ab = conn.self_identification()
            if not conn.remote_ab and self.cfg.identify_caller and not conn.closed.is_set():
                conn.remote_ab = await conn.who_are_you()
            conn.peer = number_from_answerback(conn.remote_ab) or "unbekannt"
            if self.store.is_number_blocked(conn.peer):
                result = "gesperrt (Kennung)"
                conn.seg_start = len(conn.rx_text) + 10**9   # nichts speichern
                return
            ab = self.answering_machine()
            conn.via_ab = ab["on"]
            self.register(conn)  # ab jetzt live im Chat, Antworten gehen in diese Leitung
            if conn.via_ab:
                # Anrufbeantworter: Ansage drucken, dann die Nachricht des Anrufers aufzeichnen
                conn.idle_limit = ab["idle"]
                notice = self.greeting("ab_text", conn.peer, ab["text"], zurueck=format_until(ab["until"]))
                if notice and not conn.closed.is_set():
                    await conn.send_text("\r\n" + notice + "\r\n\r\n")
            else:
                conn.idle_limit = max(30, int(self.cfg.idle_in))
                welcome = self.greeting("greet_in", conn.peer)
                if welcome and not conn.closed.is_set():
                    await conn.send_text("\r\n" + welcome + "\r\n")

            how = await self._hold_open(conn)
            if how == "timeout":
                result = "Zeitüberschreitung"
                bye = (self.greeting("ab_end", conn.peer, ab["end"], zurueck=format_until(ab["until"]))
                       if conn.via_ab else self.greeting("greet_end", conn.peer))
                if bye:
                    try:
                        await conn.send_text("\r\n" + bye + "\r\n")
                        await conn.drain_remote(timeout=20)
                    except CallFailed:
                        pass
            elif how == "hangup":
                result = "aufgelegt"

            if conn.peer == "unbekannt":
                late = conn.self_identification()
                if late:
                    conn.remote_ab = late
                    conn.peer = number_from_answerback(late) or "unbekannt"
                    for m in conn.in_mids:
                        self.store.update_message(m, peer=conn.peer, answerback=late)
                    if self.store.is_number_blocked(conn.peer):
                        for m in conn.in_mids:
                            self.store.delete_message(m)
                        conn.in_mids.clear()
                        conn.cur_in_mid = None
                        conn.seg_start = len(conn.rx_text) + 10**9
                        result = "gesperrt (Kennung)"
                        return
            await self.flush(conn, final=True)
            if conn.via_ab:
                result = ("Anrufbeantworter: Nachricht aufgezeichnet" if conn.in_mids
                          else "Anrufbeantworter: keine Nachricht")
            elif conn.in_mids:
                result = "Nachricht empfangen" if result == "angenommen" else f"Nachricht empfangen, {result}"
            elif result == "angenommen":
                result = "ohne Text beendet"
        finally:
            await self._finish(conn, result=result, started=started)

    async def _incoming_ascii(self, first, reader, writer, ip, started) -> None:
        buf = bytearray(first)
        try:
            while len(buf) < 20000:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=60)
                if not chunk:
                    break
                buf.extend(chunk)
        except (asyncio.TimeoutError, OSError):
            pass
        writer.close()
        raw = bytes(b for b in buf if b < 0x80).decode("ascii", "ignore")
        text = P.to_display(P.normalize(raw.replace("\r", "")))
        peer = number_from_answerback(text.split("\n")[0]) or "unbekannt"
        if self.store.is_number_blocked(peer):
            self.store.add_call("in", peer=peer, ip=ip, result="gesperrt (Kennung)")
            return
        if text:
            self.store.add_message("in", peer, text, status="received")
            await self.on_change("received", {"peer": peer, "text": text, "answerback": "", "name": ""})
        self.store.add_call("in", peer=peer if peer != "unbekannt" else "", ip=ip,
                            result="ASCII-Nachricht" if text else "ohne Text beendet",
                            duration=time.time() - started)
        await self.on_change("call", {})

    # --- Anmeldung an einer Vermittlung (ohne Portweiterleitung erreichbar) ----
    async def _vermittlung_loop(self) -> None:
        """Hält eine ausgehende Verbindung zur Vermittlung offen. Ruft jemand an, schickt die
        Vermittlung 83 00; wir bestätigen mit 84 00 und behandeln die Leitung als eingehenden Anruf.
        Danach meldet sich die Station neu an (wie piTelex)."""
        host, port = self.cfg.vx_host, int(self.cfg.vx_port)
        number, pin = int(self.cfg.number), int(self.cfg.tns_pin)
        fails = 0
        while True:
            delay = 2
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
                writer.write(bytes([0x81, 0x06]) + number.to_bytes(4, "little") + pin.to_bytes(2, "little"))
                await writer.drain()
                head = await asyncio.wait_for(reader.readexactly(2), timeout=10)
                body = await asyncio.wait_for(reader.readexactly(head[1]), timeout=5) if head[1] else b""
                if head[0] != 0x82:
                    reason = body.decode("ascii", "ignore").strip("\x00 ") or f"Typ {head[0]:#04x}"
                    raise ConnectionError(f"Anmeldung abgelehnt ({reason})")
                if not self.vx_status.get("ok"):
                    log.info("An der Vermittlung %s:%s angemeldet", host, port)
                self.vx_status = {"enabled": True, "host": host, "ok": True, "since": time.time()}
                fails = 0
                last_rx = time.monotonic()
                next_hb = time.monotonic() + 15
                while True:
                    timeout = max(0.5, next_hb - time.monotonic())
                    try:
                        head = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_rx > 45:
                            raise ConnectionError("Vermittlung antwortet nicht mehr")
                        writer.write(bytes([0x00, 0x00]))
                        await writer.drain()
                        next_hb = time.monotonic() + 15
                        continue
                    body = await asyncio.wait_for(reader.readexactly(head[1]), timeout=5) if head[1] else b""
                    last_rx = time.monotonic()
                    if head[0] == 0x83:
                        writer.write(bytes([0x84, 0x00]))
                        await writer.drain()
                        log.info("Anruf über die Vermittlung")
                        await self._incoming(reader, writer, via="über Vermittlung")
                        writer = None
                        break
                    if head[0] in (P.END, P.REJECT):
                        raise ConnectionError("Vermittlung hat getrennt")
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
                fails += 1
                delay = min(300, 15 * fails)
                err = str(e) or e.__class__.__name__
                if self.vx_status.get("error") != err or fails == 1:
                    log.warning("Vermittlung %s:%s: %s (neuer Versuch in %s s)", host, port, err, delay)
                self.vx_status = {"enabled": True, "host": host, "ok": False, "error": err,
                                  "time": time.time()}
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
            await asyncio.sleep(delay)

    # --- Dynamische IP beim Teilnehmerserver melden ---------------------------
    async def tns_update(self) -> None:
        if not (self.cfg.tns_dynip and self.cfg.number.isdigit() and self.cfg.tns_pin):
            return
        try:
            ip = await tns.client_update(int(self.cfg.number), int(self.cfg.tns_pin),
                                         int(self.cfg.public_port), self.cfg.tns_servers, self.cfg.tns_port)
            changed = ip != self.tns_status.get("ip")
            self.tns_status = {"enabled": True, "ok": True, "ip": ip, "time": time.time()}
            log.info("TNS-Eintrag aktualisiert, öffentliche IP %s%s", ip, " (geändert)" if changed else "")
        except tns.TNSError as e:
            self.tns_status = {**self.tns_status, "enabled": True, "ok": False, "error": str(e),
                               "time": time.time()}
            log.warning("TNS-Aktualisierung fehlgeschlagen: %s", e)

    # --- Sperrliste ----------------------------------------------------------
    async def block(self, pattern: str, note: str = "") -> int:
        pattern = pattern.strip()
        if not (pattern.isdigit() or is_ip(pattern)):
            raise ValueError("Bitte Telexnummer oder IP-Adresse angeben")
        resolved = ""
        if pattern.isdigit():
            resolved = await self._resolve_block_ip(pattern)
        return self.store.block(pattern, note, resolved)

    async def _resolve_block_ip(self, number: str) -> str:
        try:
            peer = await tns.lookup(number, self.cfg.tns_servers, self.cfg.tns_port)
        except tns.TNSError:
            return ""
        if not peer:
            return ""
        return (peer.host if is_ip(peer.host) else await tns.resolve_ip(peer.host)) or ""

    async def refresh_blocklist(self) -> None:
        for entry in self.store.blocklist():
            if entry["pattern"].isdigit():
                ip = await self._resolve_block_ip(entry["pattern"])
                if ip and ip != entry["resolved_ip"]:
                    self.store.set_resolved_ip(entry["id"], ip)
