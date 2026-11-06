"""i-Telex-Vermittlung: Teilnehmerverzeichnis (TNS) und Vermittlung (Centralex-kompatibel).

Teilnehmerverzeichnis, Port 11811 (Format wie die öffentlichen i-Telex-Teilnehmerserver):
  03 05 <Nummer u32le> <Version>            Peer_query  -> 05 64 <Eintrag> | 04 00
  01 08 <Nummer u32le> <PIN u16le> <Port>   Client_update -> 02 04 <IP>    | FF <Text>
  0A 29 <Version> <Muster 40 Byte>          Peer_search   -> 05 ... (mit Ack) 09
  06/07 05 <Version> <Server-PIN u32>        Abgleich mit anderen Teilnehmerservern (tnsproto.py)
  "q<Nummer>\\r\\n"                           ASCII-Abfrage  -> ok/fail ... +++
Eigene Teilnehmer haben Vorrang (mit Rufumleitung), dann das abgeglichene Verzeichnis, dann das
öffentliche Netz, zuletzt das gespiegelte Telefonbuch.

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
import ipaddress
import logging
import time
from dataclasses import dataclass, field

import protocol as P
import tnsproto as T
from store import Store

log = logging.getLogger("vermittlung")

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
    server_pin: int = 0                 # gesetzt = vollwertiger Teilnehmerserver mit Abgleich
    sync_servers: list[str] = field(default_factory=list)
    bind: str = "0.0.0.0"
    local_host: str = ""                # Adresse im Heimnetz: Antwort für Anfragen aus privaten Netzen


def split_server(value: str, default_port: int = 11811) -> tuple[str, int]:
    host, _, port = value.strip().rpartition(":") if ":" in value else (value.strip(), "", "")
    return (host or value.strip()), int(port) if port.isdigit() else default_port


PRIVATE_MAX_DIGITS = 4        # kürzere Nummern gibt es im öffentlichen Netz nicht: bleiben intern


def is_private(number: str) -> bool:
    """Interne Nummer (höchstens 4 Stellen): wird nie an andere Teilnehmerserver gegeben."""
    return len(str(number)) <= PRIVATE_MAX_DIGITS


def normalize_server(value: str) -> str:
    host, port = split_server(value)
    return f"{host}:{port}"


def is_local_ip(ip: str) -> bool:
    """Anfrage aus dem eigenen Netz (privat, Tailscale/CGNAT, Loopback)?"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a in ipaddress.ip_network("100.64.0.0/10")


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
        self.on_sync_change = None
        self.on_group_call = None             # Rundsenden: Anruf an eine Gruppennummer
        self.on_service_call = None           # Dienste (Plugins): Anruf an eine Dienstnummer

    async def _changed(self) -> None:
        if self.on_change:
            try:
                await self.on_change()
            except Exception:
                log.exception("Statusmeldung fehlgeschlagen")

    async def start(self) -> None:
        tns = await asyncio.start_server(self._tns, self.cfg.bind, self.cfg.tns_port)
        vx = await asyncio.start_server(self._vx, self.cfg.bind, self.cfg.vx_port)
        self._servers += [tns, vx]
        self.cfg.tns_port = tns.sockets[0].getsockname()[1]
        self.cfg.vx_port = vx.sockets[0].getsockname()[1]
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
    @property
    def sync_enabled(self) -> bool:
        return bool(self.cfg.server_pin)

    def own_record(self, entry: dict, publish: bool = False, local: bool = False) -> dict | None:
        """Eintrag im Format der Teilnehmerserver für einen eigenen Teilnehmer (ohne Umleitung).
        publish=True: auch deaktivierte und dynamische Einträge ohne IP (für den Abgleich)."""
        if not entry:
            return None
        if not entry["enabled"] and not publish:
            return None
        if entry["mode"] in ("vermittlung", "gruppe", "dienst"):
            ext = int(entry["ext_code"] or 0)
            if local and self.cfg.local_host and not publish:
                host, port = self.cfg.local_host, self.cfg.vx_port     # Heimnetz: direkt, ohne Router
            elif self.cfg.public_host:
                host, port = self.cfg.public_host, self.cfg.public_port
            else:
                return None
        else:
            host, port = entry["host"], int(entry["port"] or 134)
            ext = P.encode_extension(entry["extension"] or None)
        dyn = entry["mode"] == "dynamisch"
        if not host and not (publish and dyn):
            return None
        if dyn and not entry["ascii"]:
            etype, hostname, ip = T.T_DYNIP, "", host if T.is_ipv4(host) else ""
        elif T.is_ipv4(host):
            etype, hostname, ip = (T.T_IP_ASCII if entry["ascii"] else T.T_IP), "", host
        else:
            etype, hostname, ip = (T.T_HOST_ASCII if entry["ascii"] else T.T_HOST), host, ""
        pub = self.store.pub_get(entry["number"])
        return {"number": entry["number"], "name": entry["name"], "type": etype, "hostname": hostname,
                "ipaddress": ip, "port": port, "extension": ext,
                "pin": int(entry["pin"] or 0) if dyn else 0,
                "disabled": 0 if entry["enabled"] else 1,
                "timestamp": int(pub["timestamp"]) if pub and not pub["deleted"] else int(entry["updated"])}

    def local_target(self, rec: dict) -> tuple[str, int]:
        """Adresse zum Verbinden. Zeigt ein Eintrag auf diese Vermittlung selbst, wird intern
        verbunden (kein Umweg über den Router nötig)."""
        host, port = T.address_of(rec), int(rec.get("port") or 0)
        if self.cfg.public_host and host.lower() == self.cfg.public_host.lower() and port == self.cfg.public_port:
            return "127.0.0.1", self.cfg.vx_port
        return host, port

    def peer_reply(self, entry: dict) -> bytes | None:
        """Peer_reply für einen eigenen Eintrag ohne Umleitung (für Anzeige „im Verzeichnis“)."""
        rec = self.own_record(entry)
        return T.encode_reply(rec) if rec and T.address_of(rec) else None

    @staticmethod
    def redirect_target(entry: dict) -> str:
        ziel = (entry.get("umleitung") or "").strip()
        bis = float(entry.get("umleitung_bis") or 0)
        if ziel and (not bis or bis > time.time()):
            return ziel
        return ""

    async def lookup(self, number: str, depth: int = 0, *, upstream: bool = True,
                     local: bool = False) -> tuple[dict | None, str]:
        """Nummer auflösen: eigene Teilnehmer (mit Umleitung), abgeglichenes Verzeichnis,
        öffentliches Netz, gespiegeltes Telefonbuch. Liefert (Eintrag, Herkunft)."""
        entry = self.store.get(number)
        if entry:
            if not entry["enabled"]:
                return None, "eigener Eintrag deaktiviert"
            ziel = self.redirect_target(entry)
            if ziel:
                if depth >= 3:
                    return None, "Umleitung verschachtelt"
                rec, src = await self.lookup(ziel, depth + 1, upstream=upstream, local=local)
                if not rec:
                    return None, f"umgeleitet auf {ziel}, Ziel nicht erreichbar ({src})"
                own = self.own_record(entry, publish=True) or {}
                return ({**rec, "number": number, "name": entry["name"], "pin": 0, "disabled": 0,
                         "timestamp": max(int(rec.get("timestamp") or 0), int(own.get("timestamp") or 0))},
                        f"umgeleitet auf {ziel}")
            rec = self.own_record(entry, local=local)
            if rec and T.address_of(rec):
                return rec, "eigener Eintrag"
            why = ("öffentliche Adresse der Vermittlung fehlt" if entry["mode"] in ("vermittlung", "gruppe", "dienst")
                   else "noch keine IP gemeldet")
            return None, f"eigener Eintrag, {why}"
        vz = self.store.vz_get(number)
        usable = vz if vz and vz["type"] != T.T_DELETED and not vz["disabled"] and T.address_of(vz) else None
        if usable and vz["quelle"] == "abgleich":
            return dict(usable, pin=0), "Verzeichnis (Abgleich)"
        if upstream and self.cfg.forward:
            req = T.pkt(T.PEER_QUERY, int(number).to_bytes(4, "little") + b"\x01")
            data = await self._upstream(req)
            if data and data[0] == T.PEER_REPLY and len(data) >= 102:
                return T.decode_reply(data[2:102]), "öffentliches Netz"
            if data and data[0] == T.PEER_NOT_FOUND:
                return None, "unbekannt"
        if usable:
            return dict(usable, pin=0), "Telefonbuch"
        return None, "unbekannt" + ("" if self.cfg.forward else " (Weiterleitung aus)")

    async def public_conflict(self, number: str) -> dict | None:
        """Gibt es die Nummer schon im öffentlichen Netz? Erst das gespiegelte bzw. abgeglichene
        Verzeichnis, dann (falls erlaubt) die öffentlichen Server direkt."""
        vz = self.store.vz_get(number)
        if vz and vz["type"] != T.T_DELETED:
            return dict(vz, quelle_text="Verzeichnis")
        if is_private(number) or not self.cfg.forward:
            return None
        req = T.pkt(T.PEER_QUERY, int(number).to_bytes(4, "little") + b"\x01")
        data = await self._upstream(req)
        if data and data[0] == T.PEER_REPLY and len(data) >= 102:
            return dict(T.decode_reply(data[2:102]), quelle_text="öffentliches Netz")
        return None

    async def _upstream(self, request: bytes, ascii_query: bool = False) -> bytes | None:
        """Anfrage an das öffentliche Verzeichnis weiterreichen (mit Zwischenspeicher)."""
        if not self.cfg.forward:
            return None
        hit = self._cache.get(request)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        for server in self.cfg.upstream:
            host, port = split_server(server, self.cfg.upstream_port)
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=4)
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
                await writer.drain()
                return
            length = (await asyncio.wait_for(reader.readexactly(1), timeout=5))[0]
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
            ptype = first[0]
            if ptype == T.PEER_QUERY and length >= 4:
                await self._tns_query(payload, writer, ip)
            elif ptype == T.CLIENT_UPDATE and length == 8:
                await self._tns_update(payload, writer, ip)
            elif ptype == T.PEER_SEARCH and length >= 1:
                await self._tns_search(payload, reader, writer, ip)
            elif ptype in (T.SYNC_FULLQUERY, T.SYNC_LOGIN) and length >= 5:
                await self._tns_sync(ptype, payload, reader, writer, ip)
            else:
                log.info("Verzeichnis: unbekannte Anfrage Typ %#04x von %s", ptype, ip)
                writer.write(T.pkt_error("unknown request"))
                self.store.log("fehler", ip=ip, ok=False, detail=f"Unbekannte Anfrage Typ {ptype:#04x}")
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                OSError, ConnectionError, ValueError) as e:
            log.debug("Verzeichnis: Verbindung von %s beendet: %s", ip, e)
        finally:
            writer.close()

    async def _tns_query(self, payload: bytes, writer, ip: str) -> None:
        number = str(int.from_bytes(payload[0:4], "little"))
        rec, src = await self.lookup(number, local=is_local_ip(ip))
        if rec:
            writer.write(T.encode_reply(rec))
        else:
            writer.write(T.pkt(T.PEER_NOT_FOUND))
        self.store.log("abfrage", number=number, ip=ip, ok=bool(rec), detail=src)

    async def _tns_update(self, payload: bytes, writer, ip: str) -> None:
        number = str(int.from_bytes(payload[0:4], "little"))
        pin = int.from_bytes(payload[4:6], "little")
        port = int.from_bytes(payload[6:8], "little")
        if not T.is_ipv4(ip):
            writer.write(T.pkt_error("na"))
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="keine IPv4-Adresse")
            return
        entry = self.store.get(number)
        if entry:
            if entry["mode"] != "dynamisch" or not entry["enabled"]:
                writer.write(T.pkt_error("na"))
                self.store.log("meldung", number=number, ip=ip, ok=False, detail="kein dynamischer Eintrag")
                return
            if pin != entry["pin"]:
                writer.write(T.pkt_error("na"))
                self.store.log("meldung", number=number, ip=ip, ok=False, detail="falsche PIN")
                return
            changed = entry["host"] != ip or int(entry["port"]) != port
            self.store.set_dynamic_ip(number, ip, port)
            writer.write(T.pkt(T.ADDRESS_CONFIRM, bytes(int(x) for x in ip.split("."))))
            self.store.log("meldung", number=number, ip=ip,
                           detail=f"Port {port}" + (", Adresse geändert" if changed else ""))
            if changed:
                await self._changed()
                self._sync_changed()
            return
        # Nummer aus dem abgeglichenen Verzeichnis (nur als vollwertiger Teilnehmerserver)
        vz = self.store.vz_get(number) if self.sync_enabled else None
        if not vz or vz["quelle"] != "abgleich" or vz["type"] != T.T_DYNIP:
            writer.write(T.pkt_error("na"))
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="kein dynamischer Eintrag")
            return
        if vz["pin"] and pin != vz["pin"]:
            writer.write(T.pkt_error("na"))
            self.store.log("meldung", number=number, ip=ip, ok=False, detail="falsche PIN (Verzeichnis)")
            return
        changed = vz["ipaddress"] != ip or int(vz["port"]) != port or not vz["pin"]
        if changed:
            self.store.vz_set_ip(number, ip, port, pin, int(time.time()))
            self.store.queue_add(self.sync_peers(), number)
            self._sync_changed()
        writer.write(T.pkt(T.ADDRESS_CONFIRM, bytes(int(x) for x in ip.split("."))))
        self.store.log("meldung", number=number, ip=ip,
                       detail=f"Verzeichnis, Port {port}" + (", Adresse geändert" if changed else ""))

    async def _tns_ascii(self, line: bytes, writer, ip: str) -> None:
        number = line[1:].decode("ascii", "ignore").strip()
        rec, src = await self.lookup(number, local=is_local_ip(ip)) if number.isdigit() else (None, "ungültige Nummer")
        if rec:
            ext = P.decode_extension(int(rec.get("extension") or 0)) or "-"
            text = (f"ok\r\n{number}\r\n{rec['name']}\r\n{rec['type']}\r\n{T.address_of(rec)}\r\n"
                    f"{rec['port']}\r\n{ext}\r\n+++\r\n")
            writer.write(text.encode("iso8859-1", "replace"))
        else:
            writer.write(f"fail\r\n{number}\r\nunknown\r\n+++\r\n".encode("ascii", "replace"))
        self.store.log("abfrage", number=number, ip=ip, ok=bool(rec), detail=src + " (ASCII)")

    # --- Gesamtes Verzeichnis, Suche und Abgleich ---------------------------------
    def sync_peers(self) -> list[str]:
        return [normalize_server(s) for s in self.cfg.sync_servers] if self.sync_enabled else []

    def _sync_changed(self) -> None:
        if self.on_sync_change:
            self.on_sync_change()

    async def published_record(self, entry: dict) -> dict | None:
        """Wie ein eigener Teilnehmer im Netz steht (mit aufgelöster Umleitung)."""
        own = self.own_record(entry, publish=True)
        if entry["enabled"] and self.redirect_target(entry):
            rec, _ = await self.lookup(entry["number"])
            if rec:
                return {**rec, "pin": own["pin"] if own else 0}
        return own

    async def all_records(self, public: bool) -> list[dict]:
        """Alle bekannten Einträge: eigene Teilnehmer und Verzeichnis.
        public=True: nur sichtbare Einträge ohne PIN (für die Suche)."""
        own_numbers = set()
        out: list[dict] = []
        for entry in self.store.teilnehmer():
            own_numbers.add(entry["number"])
            if not public and is_private(entry["number"]):
                continue                       # interne Nummern verlassen den Server nicht
            rec = await self.published_record(entry)
            if not rec:
                continue
            if public and (rec["disabled"] or not T.address_of(rec)):
                continue
            out.append(dict(rec, pin=0) if public else rec)
        if not public:
            for pub in self.store.pub_all():
                if pub["deleted"] and pub["number"] not in own_numbers:
                    own_numbers.add(pub["number"])
                    out.append({"number": pub["number"], "name": "", "type": T.T_DELETED,
                                "timestamp": pub["timestamp"]})
        for vz in self.store.vz_all():
            if vz["number"] in own_numbers:
                continue
            if public and (vz["type"] == T.T_DELETED or vz["disabled"]):
                continue
            if not public and vz["quelle"] != "abgleich":
                continue                       # gespiegelte Einträge gibt nur ihr eigener Server weiter
            out.append(dict(vz, pin=0) if public else dict(vz))
        return out

    async def _tns_search(self, payload: bytes, reader, writer, ip: str) -> None:
        pattern = payload[1:41].split(b"\x00", 1)[0].decode("iso8859-1", "replace")
        words = [w.lower() for w in pattern.split() if w]
        records = [r for r in await self.all_records(public=True)
                   if all(w in (r.get("name") or "").lower() for w in words)]
        records.sort(key=lambda r: int(r["number"]))
        await T.send_list(reader, writer, records, with_pin=False)
        self.store.log("abfrage", ip=ip, detail=f"Suche „{pattern.strip()}“: {len(records)} Einträge")

    async def _tns_sync(self, ptype: int, payload: bytes, reader, writer, ip: str) -> None:
        pin = int.from_bytes(payload[1:5], "little")
        what = "Gesamtabfrage" if ptype == T.SYNC_FULLQUERY else "Anmeldung zum Abgleich"
        if not self.sync_enabled or pin != self.cfg.server_pin:
            self.store.log("abgleich", ip=ip, ok=False, detail=f"{what} mit falscher Server-PIN abgelehnt")
            log.warning("%s von %s mit falscher Server-PIN abgelehnt", what, ip)
            await asyncio.sleep(2)
            return
        if ptype == T.SYNC_FULLQUERY:
            records = await self.all_records(public=False)
            await T.send_list(reader, writer, records, with_pin=True)
            self.store.log("abgleich", ip=ip, detail=f"Gesamtabfrage: {len(records)} Einträge gesendet")
            return
        writer.write(T.pkt(T.ACK))
        await writer.drain()
        records = await T.receive_list(reader, writer)
        n = self.merge(records, von=ip)
        self.store.log("abgleich", ip=ip, detail=f"{len(records)} Einträge empfangen, {n} neu oder geändert")

    def merge(self, records: list[dict], von: str) -> int:
        """Einträge von einem anderen Teilnehmerserver übernehmen, wenn sie neuer sind."""
        changed = 0
        others = [p for p in self.sync_peers() if p != von and split_server(p)[0] != von]
        for rec in records:
            number = str(rec["number"])
            entry = self.store.get(number)
            if entry:
                changed += self._merge_own(entry, rec, others)
                continue
            cur = self.store.vz_get(number)
            if cur and cur["quelle"] == "abgleich" and int(rec["timestamp"]) <= int(cur["timestamp"]):
                continue
            if not cur and rec["type"] == T.T_DELETED:
                continue
            self.store.vz_put(rec, "abgleich", von)
            if others:
                self.store.queue_add(others, number)
            changed += 1
        if changed:
            self._sync_changed()
        return changed

    def _merge_own(self, entry: dict, rec: dict, others: list[str]) -> int:
        pub = self.store.pub_get(entry["number"])
        if pub and int(rec["timestamp"]) <= int(pub["timestamp"]):
            return 0
        if (entry["mode"] == "dynamisch" and rec["type"] == T.T_DYNIP and rec.get("ipaddress")
                and int(rec.get("pin") or 0) == int(entry["pin"])):
            if rec["ipaddress"] != entry["host"] or int(rec["port"]) != int(entry["port"]):
                self.store.set_dynamic_ip(entry["number"], rec["ipaddress"], int(rec["port"]))
                fresh = self.own_record(self.store.get(entry["number"]), publish=True)
                self.store.pub_set(entry["number"], T.fingerprint(fresh), int(rec["timestamp"]))
                if others:
                    self.store.queue_add(others, entry["number"])
                self.store.log("meldung", number=entry["number"], ip=rec["ipaddress"],
                               detail="neue IP über den Abgleich")
                return 1
            return 0
        if pub is None or T.fingerprint(rec) != pub["hash"]:
            self.store.log("abgleich", number=entry["number"], ok=False,
                           detail="Eintrag wurde im Netz geändert, hier gilt der eigene Stand")
        return 0

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
        if entry["mode"] == "dienst":
            if not self.on_service_call:
                await refuse("abs", "Dienste nicht verfügbar", number)
                return
            self.store.log("anruf", number=number, ip=ip, detail="an Dienst")
            await self.on_service_call(entry, packets, reader, writer, ip, version_sent=sent_version)
            return
        if entry["mode"] == "gruppe":
            if not self.on_group_call:
                await refuse("abs", "Rundsenden nicht verfügbar", number)
                return
            self.store.log("anruf", number=number, ip=ip, detail="an Rundsende-Gruppe")
            await self.on_group_call(entry, packets, reader, writer, ip, version_sent=sent_version)
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
