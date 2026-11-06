"""Paketformat der i-Telex-Teilnehmerserver (Port 11811).

Jedes Paket: [Typ][Länge][Daten]. Zahlen little-endian. Abgeglichen mit dem offiziellen
Teilnehmerserver (github.com/soruh/I-Telex-Teilnehmerserver, src/binaryserver/ITelexCom.ts).

  01 Client_update   8  Nummer u32, PIN u16, Port u16
  02 Address_confirm 4  IPv4
  03 Peer_query      5  Nummer u32, Version u8 (manche Geräte senden nur 4 Byte)
  04 Peer_not_found  0
  05 Peer_reply    100  Eintrag (siehe encode_reply)
  06 Sync_FullQuery  5  Version u8, Server-PIN u32  -> Gegenseite sendet alle Einträge
  07 Sync_Login      5  Version u8, Server-PIN u32  -> danach sendet man selbst Einträge
  08 Acknowledge     0
  09 End_of_List     0
  0A Peer_search    41  Version u8, Suchmuster 40 Byte (Wörter, alle müssen im Namen vorkommen)
  FF Error           n  Text

Listen (Antwort auf 06 und 0A, Einträge nach 07) laufen im Wechsel: Peer_reply, Acknowledge,
Peer_reply, Acknowledge, ... End_of_List.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress

CLIENT_UPDATE = 0x01
ADDRESS_CONFIRM = 0x02
PEER_QUERY = 0x03
PEER_NOT_FOUND = 0x04
PEER_REPLY = 0x05
SYNC_FULLQUERY = 0x06
SYNC_LOGIN = 0x07
ACK = 0x08
END_OF_LIST = 0x09
PEER_SEARCH = 0x0A
ERROR = 0xFF

# Eintragstypen
T_DELETED = 0       # gelöscht (nur im Abgleich zwischen Servern)
T_HOST = 1          # Hostname, Baudot
T_IP = 2            # feste IPv4, Baudot
T_HOST_ASCII = 3
T_IP_ASCII = 4
T_DYNIP = 5         # dynamische IPv4 (meldet sich mit PIN)
T_EMAIL = 6         # Hostname-Feld enthält eine Mailadresse (selten)
TYPE_NAMES = {0: "gelöscht", 1: "Hostname", 2: "feste IP", 3: "Hostname (ASCII)",
              4: "IP (ASCII)", 5: "dynamische IP", 6: "E-Mail"}

FLAG_DISABLED = 0x0002
EPOCH_OFFSET = 2208988800          # Sekunden zwischen 1900-01-01 und 1970-01-01
ENCODING = "iso8859-1"


def pkt(ptype: int, payload: bytes = b"") -> bytes:
    return bytes([ptype, len(payload)]) + payload


def pkt_error(text: str) -> bytes:
    return pkt(ERROR, text.encode("ascii", "replace")[:255])


def pkt_sync(ptype: int, server_pin: int) -> bytes:
    return pkt(ptype, bytes([1]) + (server_pin & 0xFFFFFFFF).to_bytes(4, "little"))


def pkt_search(pattern: str) -> bytes:
    return pkt(PEER_SEARCH, bytes([1]) + pattern.encode(ENCODING, "replace")[:40].ljust(40, b"\x00"))


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode(ENCODING, "replace").strip()


def is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def encode_reply(rec: dict, with_pin: bool = False) -> bytes:
    """Peer_reply aus einem Eintrag. Felder: number, name, type, hostname, ipaddress, port,
    extension (Rohwert 0..110), pin, disabled, timestamp (Unix-Sekunden)."""
    ip = rec.get("ipaddress") or ""
    ip_raw = bytes(int(x) for x in ip.split(".")) if is_ipv4(ip) else b"\x00\x00\x00\x00"
    ts = max(0, int(rec.get("timestamp") or 0) + EPOCH_OFFSET) & 0xFFFFFFFF
    body = (int(rec["number"]).to_bytes(4, "little")
            + str(rec.get("name") or "").encode(ENCODING, "replace")[:40].ljust(40, b"\x00")
            + (FLAG_DISABLED if rec.get("disabled") else 0).to_bytes(2, "little")
            + bytes([int(rec.get("type") or 0) & 0xFF])
            + str(rec.get("hostname") or "").encode(ENCODING, "replace")[:40].ljust(40, b"\x00")
            + ip_raw
            + (int(rec.get("port") or 0) & 0xFFFF).to_bytes(2, "little")
            + bytes([int(rec.get("extension") or 0) & 0xFF])
            + ((int(rec.get("pin") or 0) & 0xFFFF) if with_pin else 0).to_bytes(2, "little")
            + ts.to_bytes(4, "little"))
    return pkt(PEER_REPLY, body)


def decode_reply(body: bytes) -> dict:
    """Peer_reply (nur die 100 Datenbyte) in einen Eintrag umwandeln."""
    if len(body) < 100:
        raise ValueError("Peer_reply zu kurz")
    ip = ".".join(str(b) for b in body[87:91])
    return {
        "number": str(int.from_bytes(body[0:4], "little")),
        "name": _cstr(body[4:44]),
        "disabled": 1 if int.from_bytes(body[44:46], "little") & FLAG_DISABLED else 0,
        "type": body[46],
        "hostname": _cstr(body[47:87]),
        "ipaddress": "" if ip == "0.0.0.0" else ip,
        "port": int.from_bytes(body[91:93], "little"),
        "extension": body[93],
        "pin": int.from_bytes(body[94:96], "little"),
        "timestamp": max(0, int.from_bytes(body[96:100], "little") - EPOCH_OFFSET),
    }


def address_of(rec: dict) -> str:
    """Adresse, unter der der Eintrag angerufen wird (Hostname oder IP)."""
    if rec.get("type") in (T_HOST, T_HOST_ASCII, T_EMAIL):
        return rec.get("hostname") or ""
    return rec.get("ipaddress") or ""


def fingerprint(rec: dict) -> str:
    """Inhalt eines Eintrags ohne Zeitstempel (erkennt, ob sich etwas geändert hat)."""
    keys = ("number", "name", "type", "hostname", "ipaddress", "port", "extension", "pin", "disabled")
    return hashlib.sha1("|".join(str(rec.get(k) or "") for k in keys).encode()).hexdigest()


async def read(reader: asyncio.StreamReader, timeout: float = 30) -> tuple[int, bytes]:
    head = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
    payload = await asyncio.wait_for(reader.readexactly(head[1]), timeout=timeout) if head[1] else b""
    return head[0], payload


async def receive_list(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       first: tuple[int, bytes] | None = None, timeout: float = 30,
                       limit: int = 100000) -> list[dict]:
    """Einträge empfangen und jeden bestätigen, bis End_of_List kommt."""
    out: list[dict] = []
    msg = first
    while True:
        if msg is None:
            msg = await read(reader, timeout)
        ptype, payload = msg
        msg = None
        if ptype == PEER_REPLY:
            out.append(decode_reply(payload))
            if len(out) > limit:
                raise ValueError("zu viele Einträge")
            writer.write(pkt(ACK))
            await writer.drain()
        elif ptype == END_OF_LIST:
            return out
        elif ptype == ERROR:
            raise ConnectionError("Gegenstelle meldet Fehler: " + _cstr(payload))
        else:
            raise ConnectionError(f"unerwartetes Paket Typ {ptype:#04x}")


async def send_list(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, records: list[dict],
                    with_pin: bool, timeout: float = 30, wait_first_ack: bool = False) -> None:
    """Einträge senden, nach jedem auf Acknowledge warten, zuletzt End_of_List."""
    if wait_first_ack:
        await _expect_ack(reader, timeout)
    for rec in records:
        writer.write(encode_reply(rec, with_pin))
        await writer.drain()
        await _expect_ack(reader, timeout)
    writer.write(pkt(END_OF_LIST))
    await writer.drain()


async def _expect_ack(reader: asyncio.StreamReader, timeout: float) -> None:
    ptype, payload = await read(reader, timeout)
    if ptype == ERROR:
        raise ConnectionError("Gegenstelle meldet Fehler: " + _cstr(payload))
    if ptype != ACK:
        raise ConnectionError(f"Bestätigung erwartet, Typ {ptype:#04x} erhalten")
