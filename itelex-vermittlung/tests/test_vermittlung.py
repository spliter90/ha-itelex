"""Tests der Vermittlung: mehrere Server laufen lokal und sprechen über echte Sockets miteinander.

Aufruf (aus itelex-vermittlung/):  python -m unittest discover -s tests -v
Braucht aiohttp (nur für den Test der Oberfläche).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import tnsproto as T  # noqa: E402
from exchange import Config, Exchange  # noqa: E402
from store import Store  # noqa: E402
from sync import Abgleich  # noqa: E402

PIN = 4711


async def tns_request(port: int, data: bytes) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    try:
        return await T.read(reader, 5)
    finally:
        writer.close()


async def query(port: int, number: int) -> dict | None:
    ptype, payload = await tns_request(port, T.pkt(T.PEER_QUERY, number.to_bytes(4, "little") + b"\x01"))
    return T.decode_reply(payload) if ptype == T.PEER_REPLY else None


async def search(port: int, pattern: str) -> list[dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(T.pkt_search(pattern))
        await writer.drain()
        return await T.receive_list(reader, writer, timeout=5)
    finally:
        writer.close()


class Server:
    def __init__(self, tmp: str, name: str, **cfg) -> None:
        self.store = Store(os.path.join(tmp, name + ".db"))
        c = Config(bind="127.0.0.1", tns_port=0, vx_port=0, forward=cfg.pop("forward", False), **cfg)
        self.ex = Exchange(c, self.store)
        self.sync = Abgleich(self.ex, self.store)

    async def start(self) -> "Server":
        await self.ex.start()
        return self

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.ex.cfg.tns_port}"

    async def stop(self) -> None:
        await self.ex.stop()


class ProtoTest(unittest.TestCase):
    def test_roundtrip(self):
        rec = {"number": "123456", "name": "Test Ä", "type": T.T_DYNIP, "hostname": "",
               "ipaddress": "1.2.3.4", "port": 134, "extension": 17, "pin": 999, "disabled": 1,
               "timestamp": 1_700_000_000}
        raw = T.encode_reply(rec, with_pin=True)
        self.assertEqual(len(raw), 102)
        self.assertEqual(T.decode_reply(raw[2:]), rec)
        self.assertEqual(T.decode_reply(T.encode_reply(rec)[2:])["pin"], 0)

    def test_split_server(self):
        from exchange import normalize_server
        self.assertEqual(normalize_server("tlnserv.teleprinter.net"), "tlnserv.teleprinter.net:11811")
        self.assertEqual(normalize_server("1.2.3.4:999"), "1.2.3.4:999")


class NetzTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers: list[Server] = []

    async def asyncTearDown(self):
        for s in self.servers:
            await s.stop()
        self.tmp.cleanup()

    async def server(self, name: str, **cfg) -> Server:
        s = await Server(self.tmp.name, name, **cfg).start()
        self.servers.append(s)
        return s

    async def test_eigener_eintrag_und_ascii(self):
        a = await self.server("a")
        a.store.save({"number": "11111", "name": "Station A", "mode": "direkt", "host": "telex.example.org",
                      "port": 134, "extension": "5"})
        rec = await query(a.ex.cfg.tns_port, 11111)
        self.assertEqual(rec["hostname"], "telex.example.org")
        self.assertEqual(rec["type"], T.T_HOST)
        self.assertIsNone(await query(a.ex.cfg.tns_port, 99999))
        reader, writer = await asyncio.open_connection("127.0.0.1", a.ex.cfg.tns_port)
        writer.write(b"q11111\r\n")
        text = (await reader.read(500)).decode()
        writer.close()
        self.assertTrue(text.startswith("ok\r\n11111\r\nStation A\r\n1\r\ntelex.example.org\r\n134\r\n5\r\n"))

    async def test_heimnetz_antwort(self):
        a = await self.server("a", public_host="pub.example.org", local_host="10.9.8.7")
        a.store.save({"number": "33333", "name": "Ueber VX", "mode": "vermittlung"})
        rec = await query(a.ex.cfg.tns_port, 33333)            # Anfrage von 127.0.0.1 = Heimnetz
        self.assertEqual((rec["ipaddress"], rec["port"]), ("10.9.8.7", a.ex.cfg.vx_port))
        rec, _ = await a.ex.lookup("33333")                     # von außen
        self.assertEqual((rec["hostname"], rec["port"]), ("pub.example.org", 49491))
        from exchange import is_local_ip
        self.assertTrue(is_local_ip("100.72.23.101") and is_local_ip("192.168.88.5"))
        self.assertFalse(is_local_ip("178.201.192.78"))

    async def test_rufumleitung(self):
        a = await self.server("a")
        a.store.save({"number": "11111", "name": "Ziel", "mode": "direkt", "host": "10.0.0.5", "port": 134})
        a.store.save({"number": "33333", "name": "Chris", "mode": "direkt", "host": "10.0.0.9", "port": 134,
                      "umleitung": "11111"})
        rec = await query(a.ex.cfg.tns_port, 33333)
        self.assertEqual((rec["number"], rec["name"], rec["ipaddress"]), ("33333", "Chris", "10.0.0.5"))
        # Speichern ohne Umleitungsfeld behält die Umleitung
        a.store.save({"number": "33333", "name": "Chris neu", "mode": "direkt", "host": "10.0.0.9"}, "33333")
        self.assertEqual(a.store.get("33333")["umleitung"], "11111")
        # Abgelaufene Umleitung gilt nicht mehr
        a.store.set_umleitung("33333", "11111", time.time() - 1)
        self.assertEqual((await query(a.ex.cfg.tns_port, 33333))["ipaddress"], "10.0.0.9")
        # Schleife wird erkannt
        a.store.set_umleitung("33333", "11111")
        a.store.set_umleitung("11111", "33333")
        self.assertIsNone(await query(a.ex.cfg.tns_port, 33333))

    async def test_dynamische_ip(self):
        a = await self.server("a")
        e = a.store.save({"number": "22222", "name": "Dyn", "mode": "dynamisch"})
        self.assertIsNone(await query(a.ex.cfg.tns_port, 22222))
        upd = lambda pin: T.pkt(T.CLIENT_UPDATE, (22222).to_bytes(4, "little") + pin.to_bytes(2, "little")
                                + (1134).to_bytes(2, "little"))
        ptype, _ = await tns_request(a.ex.cfg.tns_port, upd((e["pin"] + 1) & 0xFFFF))
        self.assertEqual(ptype, T.ERROR)
        ptype, payload = await tns_request(a.ex.cfg.tns_port, upd(e["pin"]))
        self.assertEqual((ptype, payload), (T.ADDRESS_CONFIRM, bytes([127, 0, 0, 1])))
        rec = await query(a.ex.cfg.tns_port, 22222)
        self.assertEqual((rec["ipaddress"], rec["port"], rec["type"]), ("127.0.0.1", 1134, T.T_DYNIP))

    async def test_suche(self):
        a = await self.server("a")
        a.store.save({"number": "11111", "name": "Fablab Wuerzburg", "mode": "direkt", "host": "1.1.1.1"})
        a.store.save({"number": "11112", "name": "Museum Bonn", "mode": "direkt", "host": "1.1.1.2"})
        a.store.save({"number": "11113", "name": "Versteckt", "mode": "direkt", "host": "1.1.1.3",
                      "enabled": False})
        self.assertEqual([r["number"] for r in await search(a.ex.cfg.tns_port, " ")], ["11111", "11112"])
        self.assertEqual([r["number"] for r in await search(a.ex.cfg.tns_port, "museum")], ["11112"])

    async def test_spiegel_und_weiterleitung(self):
        pub = await self.server("pub")
        pub.store.save({"number": "44444", "name": "Oeffentlich", "mode": "direkt", "host": "2.2.2.2"})
        c = await self.server("c", upstream=[pub.addr], forward=True)
        self.assertEqual(await c.sync.mirror(), 1)
        self.assertEqual(c.store.vz_get("44444")["quelle"], "suche")
        self.assertEqual((await query(c.ex.cfg.tns_port, 44444))["ipaddress"], "2.2.2.2")
        # Öffentlicher Server weg: Antwort kommt aus dem Telefonbuch
        await pub.stop()
        self.servers.remove(pub)
        c.ex._cache.clear()
        rec, src = await c.ex.lookup("44444")
        self.assertEqual((rec["ipaddress"], src), ("2.2.2.2", "Telefonbuch"))

    async def test_abgleich(self):
        a = await self.server("a", server_pin=PIN)
        b = await self.server("b", server_pin=PIN)
        a.ex.cfg.sync_servers = [b.addr]
        b.ex.cfg.sync_servers = [a.addr]
        a.store.save({"number": "11111", "name": "Station A", "mode": "direkt", "host": "3.3.3.3"})
        e = b.store.save({"number": "22222", "name": "Station B", "mode": "dynamisch"})

        # B holt alles von A
        await a.sync.publish_own()
        self.assertEqual(await b.sync.full_query(a.addr), 1)
        self.assertEqual(b.store.vz_get("11111")["ipaddress"], "3.3.3.3")
        self.assertEqual((await query(b.ex.cfg.tns_port, 11111))["ipaddress"], "3.3.3.3")

        # B veröffentlicht seinen dynamischen Teilnehmer bei A (mit PIN)
        self.assertEqual(await b.sync.publish_own(), 1)
        await b.sync.send_queues()
        vz = a.store.vz_get("22222")
        self.assertEqual((vz["type"], vz["pin"], vz["quelle"]), (T.T_DYNIP, e["pin"], "abgleich"))
        self.assertEqual(b.store.queue_counts(), {})

        # Station meldet ihre IP bei A; A gibt sie an B weiter, B übernimmt sie für den eigenen Teilnehmer
        ptype, _ = await tns_request(a.ex.cfg.tns_port, T.pkt(T.CLIENT_UPDATE, (22222).to_bytes(4, "little")
                                     + e["pin"].to_bytes(2, "little") + (134).to_bytes(2, "little")))
        self.assertEqual(ptype, T.ADDRESS_CONFIRM)
        await asyncio.sleep(1.1)                    # Zeitstempel muss neuer sein
        a.store.vz_set_ip("22222", "127.0.0.1", 134, e["pin"], int(time.time()))
        await a.sync.send_queues()
        self.assertEqual(b.store.get("22222")["host"], "127.0.0.1")
        self.assertEqual((await query(b.ex.cfg.tns_port, 22222))["ipaddress"], "127.0.0.1")

        # Gelöschter eigener Teilnehmer wird als gelöscht weitergegeben
        a.store.delete("11111")
        await asyncio.sleep(1.1)
        self.assertEqual(await a.sync.publish_own(), 1)
        await a.sync.send_queues()
        self.assertEqual(b.store.vz_get("11111")["type"], T.T_DELETED)
        self.assertIsNone(await query(b.ex.cfg.tns_port, 11111))

        # Ältere Stände werden ignoriert
        old = dict(b.store.vz_get("11111"), type=T.T_IP, timestamp=1)
        self.assertEqual(b.ex.merge([old], von="x"), 0)

    async def test_interne_nummern_bleiben_intern(self):
        a = await self.server("a", server_pin=PIN)
        b = await self.server("b", server_pin=PIN)
        a.ex.cfg.sync_servers = [b.addr]
        a.store.save({"number": "700", "name": "Intern", "mode": "direkt", "host": "4.4.4.4"})
        a.store.save({"number": "11111", "name": "Oeffentlich", "mode": "direkt", "host": "3.3.3.3"})
        self.assertEqual(await a.sync.publish_own(), 1)
        await a.sync.send_queues()
        self.assertIsNone(b.store.vz_get("700"))
        self.assertIsNotNone(b.store.vz_get("11111"))
        self.assertEqual(await b.sync.full_query(a.addr), 0)    # 11111 schon da, 700 kommt nicht mit
        self.assertIsNone(b.store.vz_get("700"))
        # Lokal ist die interne Nummer erreichbar
        self.assertEqual((await query(a.ex.cfg.tns_port, 700))["ipaddress"], "4.4.4.4")

    async def test_falsche_server_pin(self):
        a = await self.server("a", server_pin=PIN)
        b = await self.server("b", server_pin=PIN + 1)
        a.store.save({"number": "11111", "name": "Station A", "mode": "direkt", "host": "3.3.3.3"})
        self.assertEqual(await b.sync.full_query(a.addr), 0)
        self.assertIn("Gesamtabfrage", b.sync._peer_state(a.addr)["error"])
        self.assertIsNone(b.store.vz_get("11111"))
        ev = a.store.events(kind="abgleich")
        self.assertFalse(ev[0]["ok"])

    async def test_ohne_server_pin_kein_abgleich(self):
        a = await self.server("a")
        reader, writer = await asyncio.open_connection("127.0.0.1", a.ex.cfg.tns_port)
        writer.write(T.pkt_sync(T.SYNC_FULLQUERY, 0))
        await writer.drain()
        self.assertEqual(await reader.read(10), b"")
        writer.close()


class WebTest(unittest.IsolatedAsyncioTestCase):
    async def test_anmeldung(self):
        try:
            from aiohttp.test_utils import TestClient, TestServer
        except ImportError:
            self.skipTest("aiohttp fehlt")
        from web import make_app
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "x.db"))
            ex = Exchange(Config(forward=False), store)
            app = make_app(ex, store, {}, None, {"password": "geheim", "data": tmp})
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get("/api/status")).status, 401)
                r = await client.get("/", allow_redirects=False)
                self.assertEqual(r.status, 302)
                self.assertEqual((await client.post("/api/login", json={"password": "falsch"})).status, 403)
                self.assertEqual((await client.post("/api/login", json={"password": "geheim"})).status, 200)
                self.assertEqual((await client.get("/api/status")).status, 200)
                r = await client.post("/api/teilnehmer", json={"number": "12345", "name": "X",
                                                               "mode": "direkt", "host": "1.2.3.4"})
                self.assertEqual(r.status, 200)
                r = await client.post("/api/teilnehmer/12345/umleitung", json={"ziel": "999"})
                self.assertEqual(r.status, 400)          # Ziel unbekannt
                book = await (await client.get("/api/telefonbuch?q=12345")).json()
                self.assertEqual(book["items"][0]["quelle"], "eigener Teilnehmer")
                # Nummer ist im öffentlichen Netz vergeben: Warnung, mit "trotzdem" gespeichert
                store.vz_put({"number": "55555", "name": "Fremd", "type": 1, "hostname": "x.example"}, "suche")
                body = {"number": "55555", "name": "Meins", "mode": "direkt", "host": "1.2.3.4"}
                r = await client.post("/api/teilnehmer", json=body)
                self.assertEqual(r.status, 409)
                self.assertEqual((await r.json())["name"], "Fremd")
                self.assertEqual((await client.post("/api/teilnehmer", json={**body, "trotzdem": True})).status, 200)
                # Bestehenden Teilnehmer ändern ohne Nummernwechsel: keine Warnung
                r = await client.put("/api/teilnehmer/55555", json={**body, "name": "Meins 2"})
                self.assertEqual(r.status, 200)
                r = await client.post("/api/gruppen", json={"number": "55555", "name": "G"})
                self.assertEqual(r.status, 409)
                r = await client.post("/api/gruppen", json={"number": "700", "name": "Intern"})
                self.assertEqual(r.status, 200)
                self.assertTrue((await r.json())["intern"])


if __name__ == "__main__":
    unittest.main()
