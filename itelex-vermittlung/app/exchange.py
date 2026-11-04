"""i-Telex-Vermittlung: Teilnehmerverzeichnis (TNS) und Vermittlung (Centralex-kompatibel).

Teilnehmerverzeichnis, Port 11811 (Format wie die öffentlichen i-Telex-Teilnehmerserver):
  03 05 <Nummer u32le> <Version>            Peer_query  -> 05 64 <Eintrag> | 04 00
  01 08 <Nummer u32le> <PIN u16le> <Port>   Client_update -> 02 04 <IP>    | FF <Text>
  "q<Nummer>\\r\\n"                           ASCII-Abfrage  -> ok/fail ... +++
Unbekannte Nummern werden auf Wunsch an das öffentliche Verzeichnis weitergereicht.

Vermittlung, Port 49491 (Gegenstück zum Centralex-Client von piTelex):
  Station meldet sich ausgehend an:  81 06 <Nummer u32le> <PIN u16le> -> 82 00 | 04 <Grund>
  Bereitschaft: beide Seiten senden alle 15 s 00 00.
  Anruf für die Station: 83 00 -> Station antwortet 84 00, danach wird die i-Telex-Verbindung
  des Anrufers unverändert durchgereicht.
Anrufer erreichen die Vermittlung über denselben Port. Welche Station gemeint ist, steht in der
Durchwahl, die das Verzeichnis für diese Nummer ausgibt (01–99). Die Vermittlung setzt die
Durchwahl vor dem Durchreichen auf 0 zurück.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import time
from dataclasses import dataclass, field

import protocol as P
from store import Store

log = logging.getLogger("vermittlung")

ITX_EPOCH = datetime.datetime(1900, 1, 1, tzinfo=datetime.timezone.utc)
HEARTBEAT_EVERY = 15
HEARTBEAT_TIMEOUT = 45
CACHE_SECONDS = 300


@dataclass
class Config:
    public_host: str = ""
    public_port: int = 49491
    tns_port: int = 11811
    vx_port: int = 49491
    forward: bool = True
    upstream: list[str] = field(default_factory=lambda: [
        "tlnserv.teleprinter.net", "tlnserv2.teleprinter.net", "tlnserv3.teleprinter.net"])
    upstream_port: int = 11811


def is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def peer_ip(writer: asyncio.StreamWriter) -> str:
    ip = (writer.get_extra_info("peername") or ("?",))[0]
    return ip[7:] if ip.startswith("::ffff:") else ip


@dataclass
class Client:
    """Eine an der Vermittlung angemeldete Station (wartet auf Anrufe)."""
    number: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    ip: str
    since: float = field(default_factory=time.time)
    last_hb: float = field(default_factory=time.monotonic)
    state: str = "bereit"                 # bereit / klingelt / verbunden / getrennt
    accepted: asyncio.Future | None = None
    caller: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    rx_bytes: int = 0

    def close(self) -> None:
        self.state = "getrennt"
        me = asyncio.current_task()
        for t in self.tasks:
            if t is not me:
                t.cancel()
        try:
            self.writer.close()
        except Exception:
            pass


@dataclass
class Call:
    id: int
    caller_ip: str
    number: str
    since: float = field(default_factory=time.time)
    bytes_in: int = 0
    bytes_out: int = 0


class Exchange:
    def __init__(self, cfg: Config, store: Store, on_change=None) -> None:
        self.cfg = cfg
        self.store = store
        self.on_change = on_change
        self.clients: dict[str, Client] = {}
        self.calls: dict[int, Call] = {}
        self._call_ids = 0
        self._cache: dict[bytes, tuple[float, bytes]] = {}
        self._servers: list[asyncio.base_events.Server] = []
        self._last_reg: dict[str, tuple[str, float]] = {}

    async def _changed(self) -> None:
        if self.on_change:
            try:
                await self.on_change()
            except Exception:
                log.exception("Statusmeldung fehlgeschlagen")

    async def start(self) -> None:
        self._servers.append(await asyncio.start_server(self._tns, "0.0.0.0", self.cfg.tns_port))
        self._servers.append(await asyncio.start_server(self._vx, "0.0.0.0", self.cfg.vx_port))
        log.info("Teilnehmerverzeichnis auf Port %s, Vermittlung auf Port %s",
                 self.cfg.tns_port, self.cfg.vx_port)
        if not self.cfg.public_host:
            log.warning("Keine öffentliche Adresse eingetragen: Stationen über die Vermittlung "
                        "sind im Verzeichnis nicht auffindbar")

    async def stop(self) -> None:
        for s in self._servers:
            s.close()
        for c in list(self.clients.values()):
            c.close()

    # ------------------------------------------------------------------------
    # Teilnehmerverzeichnis
    # ------------------------------------------------------------------------
    def peer_reply(self, entry: dict) -> bytes | None:
        """Peer_reply_v1 (Typ 05, 100 Byte) für einen eigenen Eintrag."""
        if not entry or not entry["enabled"]:
            return None
        if entry["mode"] == "vermittlung":
            if not self.cfg.public_host:
                return None
            host, port = self.cfg.public_host, self.cfg.public_port
            ext = entry["ext_code"]                        # 1..99 = Durchwahl "01".."99"
        else:
            host, port = entry["host"], int(entry["port"])
            ext = P.encode_extension(entry["extension"] or None)
        if not host:
            return None
        if is_ipv4(host):
            etype = (4 if entry["ascii"] else (5 if entry["mode"] == "dynamisch" else 2))
            hostname, ip = b"", bytes(int(x) for x in host.split("."))
        else:
            etype = 3 if entry["ascii"] else 1
            hostname, ip = host.encode("iso8859-1", "replace")[:40], b"\x00\x00\x00\x00"
        updated = datetime.datetime.fromtimestamp(entry["updated"], datetime.timezone.utc)
        secs = max(0, int((updated - ITX_EPOCH).total_seconds())) & 0xFFFFFFFF
        body = (int(entry["number"]).to_bytes(4, "little")
                + entry["name"].encode("iso8859-1", "replace")[:40].ljust(40, b"\x00")
                + b"\x00\x00"                                    # Flags
                + bytes([etype])
                + hostname.ljust(40, b"\x00")
                + ip
                + port.to_bytes(2, "little")
                + bytes([ext & 0xFF])
                + b"\x00\x00"                                    # PIN wird nie herausgegeben
                + secs.to_bytes(4, "little"))
        return bytes([0x05, len(body)]) + body

    async def _upstream(self, request: bytes, ascii_query: bool = False) -> bytes | None:
        """Anfrage unverändert an das öffentliche Verzeichnis weiterreichen (mit Zwischenspeicher)."""
        if not self.cfg.forward:
            return None
        hit = self._cache.get(request)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        for server in self.cfg.upstream:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(server, self.cfg.upstream_port), timeout=4)
                try:
                    writer.write(request)
                    await writer.drain()
                    if ascii_query:
                        data = b""
                        while b"+++" not in data and len(data) < 2048:
                            chunk = await asyncio.wait_for(reader.read(512), timeout=5)
                            if not chunk:
                                break
                            data += chunk
                    else:
                        head = await asyncio.wait_for(reader.readexactly(2), timeout=5)
                        data = head + (await asyncio.wait_for(reader.readexactly(head[1]), timeout=5)
                                       if head[1] else b"")
                finally:
                    writer.close()
                if data:
                    self._cache[request] = (time.monotonic(), data)
                    return data
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
                log.warning("Öffentliches Verzeichnis %s nicht erreichbar: %s", server, e)
        return None

    async def _tns(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = peer_ip(writer)
        try:
            first = await asyncio.wait_for(reader.readexactly(1), timeout=10)
            if first in (b"q", b"Q"):
                line = first + await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5)
                await self._tns_ascii(line, writer, ip)
                return
            length = (await asyncio.wait_for(reader.readexactly(1), timeout=5))[0]
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
            if first[0] == 0x03 and length >= 4:
                await self._tns_query(first + bytes([length]) + payload, payload, writer, ip)
            elif first[0] == 0x01 and length == 8:
                await self._tns_update(payload, writer, ip)
            else:
                log.info("Verzeichnis: unbekannte Anfrage Typ %#04x von %s", first[0], ip)
                writer.write(bytes([0xFF, 0x0F]) + b"unknown request")
                self.store.log("fehler", ip=ip, ok=False, detail=f"Unbekannte Anfrage Typ {first[0]:#04x}")
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            writer.close()

    async def _tns_query(self, raw: bytes, payload: bytes, writer, ip: str) -> None:
        number = str(int.from_bytes(payload[0:4], "little"))
        entry = self.store.get(number)
        reply = self.peer_reply(entry) if entry else None
        if reply:
            writer.write(reply)
            self.store.log("abfrage", number=number, ip=ip, detail="eigener Eintrag")
            return
        if entry and entry["enabled"]:
            why = ("öffentliche Adresse der Vermittlung fehlt" if entry["mode"] == "vermittlung"
                   else "noch keine IP gemeldet")
            writer.write(bytes([0x04, 0x00]))
            self.store.log("abfrage", number=number, ip=ip, ok=False, detail=f"eigener Eintrag, {why}")
            return
        data = await self._upstream(raw)
        if data:
            writer.write(data)
            found = data[0] == 0x05
            self.store.log("abfrage", number=number, ip=ip, ok=found,
                           detail="öffentliches Netz" + ("" if found else ", unbekannt"))
        else:
            writer.write(bytes([0x04, 0x00]))
            self.store.log("abfrage", number=number, ip=ip, ok=False,
                           detail="unbekannt" + ("" if self.cfg.forward else " (Weiterleitung aus)"))

    async def _tns_update(self, payload: bytes, writer, ip: str) -> None:
        number = str(int.from_bytes(payload[0:4], "little"))
        pin = int.from_bytes(payload[4:6], "little")
        port = int.from_bytes(payload[6:8], "little")
        entry = self.store.get(number)
        if not entry or entry["mode"] != "dynamisch" or not entry["enabled"]:
            writer.write(bytes([0xFF, 0x07]) + b"unknown")
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="kein dynamischer Eintrag")
            return
        if pin != entry["pin"]:
            writer.write(bytes([0xFF, 0x09]) + b"wrong pin")
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="falsche PIN")
            return
        if not is_ipv4(ip):
            writer.write(bytes([0xFF, 0x07]) + b"no ipv4")
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="keine IPv4-Adresse")
            return
        changed = entry["host"] != ip or int(entry["port"]) != port
        self.store.set_dynamic_ip(number, ip, port)
        writer.write(bytes([0x02, 0x04]) + bytes(int(x) for x in ip.split(".")))
        self.store.log("meldung", number=number, ip=ip,
                       detail=f"Port {port}" + (", Adresse geändert" if changed else ""))
        if changed:
            await self._changed()

    async def _tns_ascii(self, line: bytes, writer, ip: str) -> None:
        number = line[1:].decode("ascii", "ignore").strip()
        entry = self.store.get(number) if number.isdigit() else None
        reply = self.peer_reply(entry) if entry else None
        if reply:
            body = reply[2:]
            etype = body[46]
            host = (body[47:87].rstrip(b"\x00").decode("iso8859-1") if etype in (1, 3)
                    else ".".join(str(b) for b in body[87:91]))
            ext = P.decode_extension(body[93]) or "-"
            text = (f"ok\r\n{number}\r\n{entry['name']}\r\n{etype}\r\n{host}\r\n"
                    f"{int.from_bytes(body[91:93], 'little')}\r\n{ext}\r\n+++\r\n")
            writer.write(text.encode("iso8859-1", "replace"))
            self.store.log("abfrage", number=number, ip=ip, detail="eigener Eintrag (ASCII)")
            return
        data = await self._upstream(line, ascii_query=True) if number.isdigit() and not entry else None
        if data:
            writer.write(data)
            self.store.log("abfrage", number=number, ip=ip, ok=data.startswith(b"ok"),
                           detail="öffentliches Netz (ASCII)")
        else:
            writer.write(f"fail\r\n{number}\r\nunknown\r\n+++\r\n".encode("ascii", "replace"))
            self.store.log("abfrage", number=number, ip=ip, ok=False, detail="unbekannt (ASCII)")

    # ------------------------------------------------------------------------
    # Vermittlung
    # ------------------------------------------------------------------------
    async def _vx(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = peer_ip(writer)
        try:
            head = await asyncio.wait_for(reader.readexactly(2), timeout=15)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return
        if head[0] == 0x81:
            await self._register(head, reader, writer, ip)
        elif head[0] in P.KNOWN_TYPES:
            await self._call(head, reader, writer, ip)
        else:
            log.info("Vermittlung: unbekannte Daten von %s (%s)", ip, head.hex())
            writer.close()

    async def _register(self, head: bytes, reader, writer, ip: str) -> None:
        try:
            payload = await asyncio.wait_for(reader.readexactly(head[1]), timeout=5) if head[1] else b""
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return
        if len(payload) < 6:
            writer.close()
            return
        number = str(int.from_bytes(payload[0:4], "little"))
        pin = int.from_bytes(payload[4:6], "little")
        entry = self.store.get(number)
        reason = None
        if not entry or entry["mode"] != "vermittlung":
            reason, detail = "na", "Nummer nicht für die Vermittlung eingetragen"
        elif not entry["enabled"]:
            reason, detail = "abs", "Eintrag deaktiviert"
        elif pin != entry["pin"]:
            reason, detail = "na", "falsche PIN"
        if reason:
            try:
                writer.write(P.pkt_reject(reason))
                await writer.drain()
            except OSError:
                pass
            writer.close()
            self.store.log("anmeldung", number=number, ip=ip, ok=False, detail=detail)
            log.info("Anmeldung %s von %s abgelehnt: %s", number, ip, detail)
            return

        old = self.clients.get(number)
        if old and old.state in ("bereit", "getrennt"):
            old.close()
        elif old:
            # Station hat während eines Gesprächs neu angemeldet: altes Gespräch bleibt bestehen,
            # die neue Verbindung übernimmt die Bereitschaft.
            pass
        writer.write(bytes([0x82, 0x00]))
        await writer.drain()
        client = Client(number, reader, writer, ip)
        self.clients[number] = client
        client.tasks.append(asyncio.create_task(self._client_reader(client)))
        client.tasks.append(asyncio.create_task(self._client_heartbeat(client)))
        prev = self._last_reg.get(number)
        quiet = prev is not None and prev[0] == ip and time.time() - prev[1] < 600
        self._last_reg[number] = (ip, time.time())
        if not quiet:
            self.store.log("anmeldung", number=number, ip=ip, detail="angemeldet")
            log.info("Station %s ist über die Vermittlung erreichbar (%s)", number, ip)
        await self._changed()

    async def _client_heartbeat(self, client: Client) -> None:
        try:
            while client.state == "bereit":
                await asyncio.sleep(HEARTBEAT_EVERY)
                if client.state != "bereit":
                    break
                if time.monotonic() - client.last_hb > HEARTBEAT_TIMEOUT:
                    log.info("Station %s antwortet nicht mehr", client.number)
                    self._drop(client)
                    await self._changed()
                    break
                client.writer.write(bytes([0x00, 0x00]))
                await client.writer.drain()
        except (OSError, ConnectionError):
            self._drop(client)
            await self._changed()
        except asyncio.CancelledError:
            pass

    def _drop(self, client: Client) -> None:
        client.close()
        if self.clients.get(client.number) is client:
            del self.clients[client.number]

    async def _client_reader(self, client: Client) -> None:
        """Liest alles, was die angemeldete Station schickt. Im Gespräch: direkt zum Anrufer."""
        r = client.reader
        try:
            while True:
                if client.state == "verbunden":
                    data = await r.read(4096)
                    if not data:
                        break
                    client.rx_bytes += len(data)
                    if client.caller:
                        client.caller.write(data)
                        await client.caller.drain()
                    continue
                head = await r.readexactly(2)
                payload = await r.readexactly(head[1]) if head[1] else b""
                if head[0] == 0x00:
                    client.last_hb = time.monotonic()
                elif head[0] == 0x84 and client.accepted and not client.accepted.done():
                    client.state = "verbunden"
                    client.accepted.set_result(True)
                elif head[0] == P.END:
                    reason = payload.decode("ascii", "ignore").strip("\x00 ")
                    log.info("Station %s meldet sich ab%s", client.number, f" ({reason})" if reason else "")
                    break
                elif head[0] == P.REJECT and client.accepted and not client.accepted.done():
                    client.accepted.set_result(False)
        except (asyncio.IncompleteReadError, OSError, ConnectionError):
            pass
        except asyncio.CancelledError:
            return
        finally:
            if client.accepted and not client.accepted.done():
                client.accepted.set_result(False)
            if client.caller:
                try:
                    client.caller.close()
                except Exception:
                    pass
            was_registered = self.clients.get(client.number) is client
            self._drop(client)
            if was_registered:
                await self._changed()

    async def _call(self, head: bytes, reader, writer, ip: str) -> None:
        started = time.time()

        async def refuse(reason: str, detail: str, number: str = "") -> None:
            try:
                writer.write(P.pkt_reject(reason))
                await writer.drain()
            except OSError:
                pass
            writer.close()
            self.store.log("anruf", number=number, ip=ip, ok=False, detail=detail)
            log.info("Anruf von %s%s abgewiesen: %s", ip, f" an {number}" if number else "", detail)

        # Pakete des Anrufers sammeln, bis die Durchwahl da ist
        packets: list[tuple[int, bytes]] = []
        ptype, plen = head[0], head[1]
        ext_value: int | None = None
        sent_version = False
        deadline = time.monotonic() + 10
        try:
            while True:
                payload = await asyncio.wait_for(reader.readexactly(plen), timeout=5) if plen else b""
                packets.append((ptype, payload))
                if ptype == P.DIRECT_DIAL and payload:
                    ext_value = payload[0]
                    break
                if ptype in (P.BAUDOT, P.END, P.REJECT) or len(packets) > 8:
                    break
                wait = 2.0 if not sent_version else max(0.5, deadline - time.monotonic())
                try:
                    nxt = await asyncio.wait_for(reader.readexactly(2), timeout=wait)
                except asyncio.TimeoutError:
                    if not sent_version and any(t == P.VERSION for t, _ in packets):
                        # Manche Geräte warten auf unsere Version, bevor sie die Durchwahl senden
                        writer.write(P.pkt_version())
                        await writer.drain()
                        sent_version = True
                        continue
                    break
                ptype, plen = nxt[0], nxt[1]
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return

        code = P.decode_extension(ext_value) if ext_value is not None else None
        if not code or not code.isdigit() or len(code) != 2 or int(code) == 0:
            await refuse("na", "ohne gültige Durchwahl")
            return
        entry = self.store.by_ext_code(int(code))
        if not entry:
            await refuse("na", f"Durchwahl {code} nicht vergeben")
            return
        number = entry["number"]
        if not entry["enabled"]:
            await refuse("abs", "Eintrag deaktiviert", number)
            return
        client = self.clients.get(number)
        if not client:
            await refuse("nc", "Station nicht angemeldet", number)
            return
        if client.state != "bereit":
            await refuse("occ", "Station besetzt", number)
            return

        # Station klingeln lassen
        client.state = "klingelt"
        client.accepted = asyncio.get_running_loop().create_future()
        client.caller = writer
        try:
            client.writer.write(bytes([0x83, 0x00]))
            await client.writer.drain()
            ok = await asyncio.wait_for(asyncio.shield(client.accepted), timeout=10)
        except (asyncio.TimeoutError, OSError):
            ok = False
        if not ok:
            client.caller = None
            self._drop(client)
            await refuse("nc", "Station nimmt nicht an", number)
            await self._changed()
            return

        self._call_ids += 1
        call = Call(self._call_ids, ip, number)
        self.calls[call.id] = call
        await self._changed()
        log.info("Anruf von %s an %s wird vermittelt", ip, number)
        try:
            # Gesammelte Pakete weiterreichen, Durchwahl auf "keine" setzen
            out = bytearray()
            for t, p in packets:
                out += P.pkt_direct_dial(None) if t == P.DIRECT_DIAL else bytes([t, len(p)]) + p
            client.writer.write(bytes(out))
            await client.writer.drain()
            call.bytes_in += len(out)
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                call.bytes_in += len(data)
                client.writer.write(data)
                await client.writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            call.bytes_out = client.rx_bytes
            self.calls.pop(call.id, None)
            self._drop(client)
            try:
                writer.close()
            except Exception:
                pass
            dur = time.time() - started
            self.store.log("anruf", number=number, ip=ip, duration=dur,
                           detail=f"vermittelt, {call.bytes_in} / {call.bytes_out} Byte")
            log.info("Anruf an %s beendet nach %.0f s", number, dur)
            await self._changed()

    # ------------------------------------------------------------------------
    def status(self) -> dict:
        now = time.time()
        return {
            "online": [{"number": c.number, "ip": c.ip, "since": c.since, "state": c.state}
                       for c in self.clients.values()],
            "calls": [{"id": c.id, "caller_ip": c.caller_ip, "number": c.number, "since": c.since,
                       "bytes": c.bytes_in} for c in self.calls.values()],
            "time": now,
        }
