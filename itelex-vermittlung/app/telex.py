"""i-Telex-Leitung für die Vermittlung: Fernschreiben senden und empfangen (Binärprotokoll).

Gekürzte Fassung der Verbindungslogik aus itelex/app/station.py (Station-Add-on): Flusskontrolle
über die Druckbestätigungen, „Wer da?“ mit Einsammeln der Kennung, Bestätigung jede Sekunde.
Die beiden Add-ons werden getrennt gebaut, deshalb liegt der Code hier noch einmal.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import protocol as P

log = logging.getLogger("vermittlung.telex")

READY_TIMEOUT = 25        # Sekunden, bis die Gegenstelle ihren Fernschreiber gestartet hat
STALL_TIMEOUT = 40        # Sekunden ohne Druckfortschritt -> Abbruch
MAX_UNPRINTED = 8         # so viele Zeichen dürfen ungedruckt unterwegs sein
CHUNK = 10                # Zeichen pro Baudot-Paket
_NUM_RE = re.compile(r"\d{3,}")


class CallFailed(Exception):
    def __init__(self, code: str, text: str | None = None):
        self.code = code
        super().__init__(text or P.REJECT_REASONS.get(code, code))


def number_from_answerback(answerback: str) -> str:
    m = _NUM_RE.search(answerback or "")
    return m.group(0) if m else ""


def clean_answerback(text: str) -> str:
    text = text.replace(P.WRU, " ").replace(P.BELL, " ")
    return re.sub(r"\s+", " ", text).strip()


async def first_of(*events: asyncio.Event, timeout: float) -> None:
    tasks = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


class Line:
    """Eine i-Telex-Verbindung. server=True: wir wurden angerufen."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, server: bool,
                 answerback: str) -> None:
        self.reader, self.writer, self.server = reader, writer, server
        self.answerback = answerback
        self.enc = P.BaudotEncoder()
        self.dec = P.BaudotDecoder()
        self.sent = 0
        self.remote_printed: int | None = None
        self.received = 0
        self.rx_text = ""
        self.last_rx = time.monotonic()
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        self.progress = asyncio.Event()
        self.closed = asyncio.Event()
        self.reject: str | None = None
        self._version_sent = False
        self._wlock = asyncio.Lock()
        self._tlock = asyncio.Lock()
        self.answerback_spans: list[tuple[int, int]] = []
        self.answerbacks: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    # --- Senden ------------------------------------------------------------------
    async def write(self, data: bytes) -> None:
        if self.closed.is_set():
            raise CallFailed("nc", "Verbindung getrennt")
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

    async def _wait_progress(self, timeout: float) -> None:
        self.progress.clear()
        try:
            await asyncio.wait_for(self.progress.wait(), timeout)
        except asyncio.TimeoutError:
            pass

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
        async with self._tlock:
            await self.send_baudot(self.enc.encode(text))

    async def drain_remote(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.unprinted() and time.monotonic() < deadline and not self.closed.is_set():
            await self._wait_progress(1.0)
        return self.unprinted() == 0

    async def send_own_answerback(self) -> None:
        if self.answerback:
            await self.send_text("\r\n" + P.normalize(self.answerback) + "\r\n")

    async def who_are_you(self, wait: float = 8.0) -> str:
        """Wer da? senden und die Kennung der Gegenstelle einsammeln."""
        await self.send_text(P.WRU)
        start = len(self.rx_text)
        a = end = start
        found = False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not self.closed.is_set():
            await self._wait_progress(0.4)
            seg = self.rx_text[start:]
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
        lines = [ln for ln in text.split("\n") if clean_answerback(ln) not in self.answerbacks]
        return P.to_display("\n".join(lines))

    # --- Empfangen -----------------------------------------------------------------
    def start(self, preloaded: list[tuple[int, bytes]] | None = None) -> None:
        self._tasks.append(asyncio.create_task(self._reader_loop(preloaded or [])))
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))

    async def _keepalive_loop(self) -> None:
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

    async def _reader_loop(self, preloaded: list[tuple[int, bytes]]) -> None:
        try:
            for ptype, payload in preloaded:
                await self._handle(ptype, payload)
            while not self.closed.is_set():
                ptype, payload = await P.read_packet(self.reader)
                await self._handle(ptype, payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, CallFailed):
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
            await self._start_printer()           # Durchwahl hat die Vermittlung schon ausgewertet
        elif ptype == P.BAUDOT:
            text = self.dec.decode(payload)
            self.received += len(payload)
            self.rx_text += text
            self.last_rx = time.monotonic()
            self.ready.set()
            if self.server and not self.started.is_set():
                await self._start_printer()
            await self.write(P.pkt_ack(self.received))
            if P.WRU in text:
                asyncio.create_task(self._answer_wru())
            self.progress.set()
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
            if self.server and not self._version_sent:
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
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()


@dataclass
class Target:
    host: str
    port: int
    extension: str | None = None
    ascii: bool = False


@dataclass
class Result:
    ok: bool
    code: str = ""                # bei Fehler: occ / nc / der / abs / na / ...
    detail: str = ""
    answerback: str = ""          # Kennung der Gegenstelle nach dem Text (Zustellbestätigung)


async def send(target: Target, text: str, answerback: str) -> Result:
    """Ein Fernschreiben zustellen: verbinden, Wer da?, eigene Kennung, Text, Wer da?."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(target.host, target.port), 10)
    except (OSError, asyncio.TimeoutError):
        return Result(False, "nc", "nicht erreichbar")
    body = P.wrap(P.normalize(text))
    if target.ascii:
        try:
            writer.write(("\r\n" + P.normalize(answerback) + "\r\n" + body + "\r\n").encode("ascii", "replace"))
            await writer.drain()
            await asyncio.sleep(3)
        except OSError:
            return Result(False, "nc", "Verbindung abgebrochen")
        finally:
            writer.close()
        return Result(True, detail="übertragen (ASCII)")
    line = Line(reader, writer, server=False, answerback=answerback)
    try:
        line.start()
        await line.send_version()
        await line.write(P.pkt_direct_dial(target.extension))
        await first_of(line.ready, line.closed, timeout=READY_TIMEOUT)
        if line.reject:
            raise CallFailed(line.reject)
        if not line.ready.is_set():
            raise CallFailed("der", "Fernschreiber startet nicht")
        await line.send_baudot(line.enc.force_letters())
        remote = await line.who_are_you()
        await line.send_own_answerback()
        await line.send_text("\r\n" + body + "\r\n\r\n")
        confirm = await line.who_are_you(wait=10)
        await line.drain_remote(timeout=30 + len(body) * 0.2)
        if line.reject:
            raise CallFailed(line.reject)
        return Result(True, detail="zugestellt" if confirm else "übertragen (ohne Bestätigung)",
                      answerback=confirm or remote)
    except CallFailed as e:
        code = e.code
        if line.closed.is_set() and code == "nc" and not line.reject:
            return Result(False, "nc", "Gegenstelle hat aufgelegt")
        return Result(False, code, str(e))
    finally:
        await line.close()
