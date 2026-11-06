"""Tests für Rundsenden: Test-Fernschreiber empfangen, ein Test-Absender ruft die Gruppennummer an."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import protocol as P  # noqa: E402
import rundsenden as R  # noqa: E402
import telex  # noqa: E402
from exchange import Config, Exchange  # noqa: E402
from store import Store  # noqa: E402


class FakeStation:
    """Nimmt Fernschreiben an und merkt sich den empfangenen Text."""

    def __init__(self, answerback: str) -> None:
        self.answerback = answerback
        self.texts: list[str] = []
        self.got = asyncio.Event()

    async def start(self) -> "FakeStation":
        self.server = await asyncio.start_server(self._conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _conn(self, reader, writer) -> None:
        line = telex.Line(reader, writer, server=True, answerback=self.answerback)
        line.start()
        await line.closed.wait()
        self.texts.append(line.rx_text)
        self.got.set()

    def stop(self) -> None:
        self.server.close()


async def call_group(port: int, ext: str, answerback: str, text: str, wait_greeting: bool = True) -> str:
    """Gruppennummer anrufen, Text senden, Antwort der Gruppe zurückgeben."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    line = telex.Line(reader, writer, server=False, answerback=answerback)
    line.start()
    await line.send_version()
    await line.write(P.pkt_direct_dial(ext))
    await telex.first_of(line.ready, line.closed, timeout=10)
    if wait_greeting:
        for _ in range(100):
            if "MOEGLICH" in line.rx_text or "SENDEN" in line.rx_text or "BERECHTIGT" in line.rx_text:
                break
            await asyncio.sleep(0.1)
    if "NICHT BERECHTIGT" not in line.rx_text:
        await line.send_text(text + "\r\nNNNN\r\n")
        for _ in range(100):
            if any(w in line.rx_text for w in ("UEBERNOMMEN", "ANGEMELDET", "ABGEMELDET", "NICHT")):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
    await line.close()
    return line.rx_text


class RundsendenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        R.RETRY_DELAYS = [0.2, 0.2]
        self.store = Store(os.path.join(self.tmp.name, "a.db"))
        self.ex = Exchange(Config(bind="127.0.0.1", tns_port=0, vx_port=0, forward=False), self.store)
        await self.ex.start()
        self.rs = R.Rundsenden(self.ex, self.store)
        self.st1 = await FakeStation("11111 eins d").start()
        self.st2 = await FakeStation("22222 zwei d").start()
        self.sender = await FakeStation("44444 sender d").start()
        for nr, st in (("11111", self.st1), ("22222", self.st2), ("44444", self.sender)):
            self.store.save({"number": nr, "name": f"Station {nr}", "mode": "direkt", "host": "127.0.0.1",
                             "port": st.port})
        # 33333 zeigt auf einen geschlossenen Port: nicht erreichbar
        self.store.save({"number": "33333", "name": "Weg", "mode": "direkt", "host": "127.0.0.1", "port": 1})
        self.g = self.store.gruppe_save({"number": "70000", "name": "Testgruppe", "kennung": "70000 test d",
                                         "berechtigung": "alle", "selbst": True})
        for nr in ("11111", "22222", "33333"):
            self.store.mitglied_add("70000", nr)
        self.ext = f"{self.g['ext_code']:02d}"

    async def asyncTearDown(self):
        await self.rs.stop()
        await self.ex.stop()
        for st in (self.st1, self.st2, self.sender):
            st.stop()
        self.tmp.cleanup()

    async def wait_done(self, rid: int, timeout: float = 30) -> dict:
        for _ in range(int(timeout * 10)):
            rs = self.store.rs_get(rid)
            if rs["status"] == "fertig":
                return rs
            await asyncio.sleep(0.1)
        self.fail("Rundsendung wurde nicht fertig")

    async def test_rundsendung_per_anruf_mit_bericht(self):
        reply = await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "HALLO ALLE")
        self.assertIn("RUNDSENDEN TESTGRUPPE", reply)
        self.assertIn("UEBERNOMMEN NR 1", reply)
        rs = await self.wait_done(1)
        status = {z["number"]: z["status"] for z in rs["zustellungen"]}
        self.assertEqual(status, {"11111": "zugestellt", "22222": "zugestellt", "33333": "fehler"})
        self.assertEqual([z["tries"] for z in rs["zustellungen"] if z["number"] == "33333"], [3])
        self.assertEqual(rs["absender"], "44444")
        for st in (self.st1, self.st2):
            self.assertIn("HALLO ALLE", st.texts[0])
            self.assertIn("RUNDSENDUNG TESTGRUPPE (70000) NR 1", st.texts[0])
            self.assertIn("70000 TEST D", st.texts[0])         # Kennung der Gruppe
        self.assertEqual(rs["bericht"], "Bericht zugestellt")
        report = self.sender.texts[-1]
        self.assertIn("ZUGESTELLT: 2 VON 3", report)
        self.assertIn("33333", report)

    async def test_text_sofort_nach_verbindung(self):
        await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "SCHNELL", wait_greeting=False)
        rs = await self.wait_done(1)
        self.assertEqual(rs["text"], "SCHNELL")

    async def test_nur_mitglieder(self):
        self.store.gruppe_save({**self.g, "berechtigung": "mitglieder", "selbst": False}, original="70000")
        reply = await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "DARF NICHT")
        self.assertIn("NICHT BERECHTIGT", reply)
        self.assertEqual(self.store.rs_list(), [])
        # Als Mitglied darf er, bekommt seine eigene Rundsendung aber nicht
        self.store.mitglied_add("70000", "44444")
        reply = await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "JETZT ABER")
        self.assertIn("UEBERNOMMEN", reply)
        rs = await self.wait_done(1)
        self.assertNotIn("44444", [z["number"] for z in rs["zustellungen"]])

    async def test_freigegebene_nummern(self):
        self.store.gruppe_save({**self.g, "berechtigung": "liste", "erlaubt": "44444, 55555"}, original="70000")
        self.assertTrue(self.rs.may_send(self.store.gruppe("70000"), "44444"))
        self.assertFalse(self.rs.may_send(self.store.gruppe("70000"), "66666"))
        self.assertTrue(self.rs.may_send(self.store.gruppe("70000"), "11111"))   # Mitglied

    async def test_anmelden_abmelden(self):
        self.store.gruppe_save({**self.g, "berechtigung": "mitglieder"}, original="70000")
        reply = await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "ANMELDEN")
        self.assertIn("44444 ANGEMELDET", reply)
        self.assertTrue(self.store.ist_mitglied("70000", "44444"))
        self.assertEqual(self.store.rs_list(), [])
        reply = await call_group(self.ex.cfg.vx_port, self.ext, "44444 sender d", "ABMELDEN")
        self.assertIn("44444 ABGEMELDET", reply)
        self.assertFalse(self.store.ist_mitglied("70000", "44444"))

    async def test_aus_der_verwaltung(self):
        self.store.gruppe_save({**self.g, "kopf": False}, original="70000")
        rid = self.rs.broadcast("70000", "AUS DER VERWALTUNG")
        rs = await self.wait_done(rid)
        self.assertEqual(rs["bericht"], "")
        self.assertIn("AUS DER VERWALTUNG", self.st1.texts[0])
        self.assertNotIn("RUNDSENDUNG", self.st1.texts[0])
        with self.assertRaises(ValueError):
            self.rs.broadcast("70000", "   ")

    async def test_gruppe_im_verzeichnis(self):
        self.ex.cfg.public_host = "telex.example.org"
        rec, _ = await self.ex.lookup("70000")
        self.assertEqual((rec["hostname"], rec["port"], P.decode_extension(rec["extension"])),
                         ("telex.example.org", self.ex.cfg.public_port, self.ext))
        with self.assertRaises(ValueError):
            self.store.mitglied_add("70000", "70000")


if __name__ == "__main__":
    unittest.main()
