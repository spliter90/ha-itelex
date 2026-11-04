"""Abfrage des i-Telex-Teilnehmerservers (TNS), binäres Peer_query-Paket."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

log = logging.getLogger("itelex.tns")


@dataclass
class Peer:
    number: str
    name: str
    host: str
    port: int
    extension: str | None
    ascii: bool


class TNSError(Exception):
    pass


async def _query_one(server: str, port: int, number: int) -> Peer | None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(server, port), timeout=4)
    try:
        writer.write(bytes([0x03, 0x05]) + number.to_bytes(4, "little") + b"\x01")
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(2), timeout=5)
        if head[0] == 0x04:  # Peer_not_found
            return None
        if head[0] != 0x05 or head[1] != 0x64:
            raise TNSError(f"unerwartete Antwort {head.hex()}")
        data = await asyncio.wait_for(reader.readexactly(0x64), timeout=5)
    finally:
        writer.close()

    from protocol import decode_extension

    num = str(int.from_bytes(data[0:4], "little"))
    name = data[4:44].decode("iso8859-1").rstrip("\x00").strip()
    etype = data[46]
    hostname = data[47:87].decode("iso8859-1").rstrip("\x00").strip()
    ip = ".".join(str(b) for b in data[87:91])
    tcp_port = int.from_bytes(data[91:93], "little")
    ext = decode_extension(data[93])

    if etype in (1, 2, 5):
        is_ascii = False
    elif etype in (3, 4):
        is_ascii = True
    else:
        return None  # 0 = gelöscht, 6 = E-Mail: nicht unterstützt
    host = hostname if etype in (1, 3) else ip
    return Peer(num, name, host, tcp_port, ext, is_ascii)


async def lookup(number: str, servers: list[str], port: int = 11811) -> Peer | None:
    try:
        n = int(number)
    except ValueError as e:
        raise TNSError("Nummer darf nur Ziffern enthalten") from e
    if not 0 < n <= 0xFFFFFFFF:
        raise TNSError("Nummer außerhalb des gültigen Bereichs")

    # Reihenfolge wie eingetragen: der erste erreichbare Server antwortet (z. B. eigene Vermittlung)
    last_err: Exception | None = None
    for server in servers:
        try:
            return await _query_one(server, port, n)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, TNSError) as e:
            log.warning("TNS %s nicht erreichbar: %s", server, e)
            last_err = e
    raise TNSError(f"kein Teilnehmerserver erreichbar ({last_err})")


async def resolve_ip(host: str) -> str | None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=2)
        return infos[0][4][0] if infos else None
    except OSError:
        return None


async def client_update(number: int, pin: int, port: int, servers: list[str],
                        tns_port: int = 11811) -> str:
    """Eigenen Eintrag beim TNS aktualisieren (Client_update) und die vom TNS gesehene IP liefern.
    Format wie piTelex/i-Telex: 01 08 <Nummer u32le> <PIN u16le> <Port u16le> -> 02 04 <IP>."""
    order = list(servers)  # in Reihenfolge; lehnt ein Server ab, wird der nächste versucht
    payload = (bytes([0x01, 0x08]) + number.to_bytes(4, "little") + pin.to_bytes(2, "little")
               + port.to_bytes(2, "little"))
    last_err: Exception | None = None
    for server in order:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(server, tns_port), timeout=4)
            try:
                writer.write(payload)
                await writer.drain()
                head = await asyncio.wait_for(reader.readexactly(2), timeout=5)
                body = await asyncio.wait_for(reader.readexactly(head[1]), timeout=5) if head[1] else b""
            finally:
                writer.close()
            if head[0] == 0x02 and len(body) == 4:
                return ".".join(str(b) for b in body)
            raise TNSError(f"TNS lehnt ab (Typ {head[0]:#04x} {body.decode('ascii', 'ignore')!r})")
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, TNSError) as e:
            log.warning("TNS %s: %s", server, e)
            last_err = e
    raise TNSError(f"Aktualisierung fehlgeschlagen ({last_err})")
