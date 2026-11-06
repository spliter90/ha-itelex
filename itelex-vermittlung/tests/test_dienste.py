"""Tests für anrufbare Dienste: Plugin-Erkennung, Burg-Abenteuer, echte Anrufe über die Vermittlung."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import protocol as P  # noqa: E402
import telex  # noqa: E402
from dienste import Dienste, burg, plugins  # noqa: E402
from exchange import Config, Exchange  # noqa: E402
from store import Store  # noqa: E402

LOESUNG = ["O", "HOCH", "RUNTER", "O", "UNTERSUCHE HEU", "NIMM SCHLUESSEL", "W", "W", "NIMM FACKEL",
           "DREHE KURBEL", "O", "OEFFNE TUER", "N", "W", "NIMM SCHINKEN", "O", "GIB DEM HUND DEN SCHINKEN",
           "RUNTER", "O", "NIMM SIEGEL", "W", "HOCH", "S", "S"]


class FakeSitzung:
    def __init__(self, eingaben: list[str], speicher: dict | None = None) -> None:
        self.eingaben = list(eingaben)
        self.speicher = speicher if speicher is not None else {}
        self.ausgabe: list[str] = []
        self.verbunden = True

    async def schreibe(self, text: str = "") -> None:
        self.ausgabe.append(P.normalize(text))

    async def lies(self, timeout: float = 0) -> str | None:
        return self.eingaben.pop(0).upper() if self.eingaben else None

    @property
    def text(self) -> str:
        return "\n".join(self.ausgabe)


class BurgTest(unittest.IsolatedAsyncioTestCase):
    def spiel(self, zuege: list[str]) -> tuple[burg.Spiel, list[str]]:
        sp = burg.Spiel(burg.neues_spiel())
        out = []
        for z in zuege:
            sp.st["zuege"] += 1
            out.append(sp.zug(z) + "\n" + sp.nach_zug())
        return sp, out

    def test_loesung(self):
        sp, out = self.spiel(LOESUNG)
        self.assertEqual(sp.st["ende"], "gewonnen", "\n---\n".join(out[-5:]))
        self.assertIn("GEWONNEN. Züge: 24", out[-1])

    def test_ohne_gitter_knapp_entkommen(self):
        zuege = [z for z in LOESUNG[:LOESUNG.index("NIMM SIEGEL") + 1] if z != "DREHE KURBEL"]  # ohne Gitter
        sp, _ = self.spiel(zuege + ["W", "HOCH", "S"])
        self.assertIn("Fallgitter ist unten", sp.zug("S"))
        sp, out = self.spiel(zuege + ["W", "HOCH", "S", "W", "DREHE KURBEL", "O", "S"])
        self.assertEqual(sp.st["ende"], "gewonnen", out[-1])

    def test_gefasst_bei_zu_langem_zoegern(self):
        zuege = LOESUNG[:LOESUNG.index("NIMM SIEGEL") + 1]
        sp, out = self.spiel(zuege + ["WARTE"] * 8)
        self.assertEqual(sp.st["ende"], "gefasst")
        self.assertTrue(any("GEFASST" in o for o in out))

    def test_wehrgang_bei_alarm(self):
        zuege = LOESUNG[:LOESUNG.index("NIMM SIEGEL") + 1] + ["W", "HOCH", "S", "HOCH"]
        sp, _ = self.spiel(zuege)
        self.assertEqual(sp.st["ende"], "gefasst")

    def test_hindernisse(self):
        sp, out = self.spiel(["O", "HOCH", "RUNTER", "N"])
        self.assertIn("verschlossen", out[-1])
        sp = burg.Spiel(dict(burg.neues_spiel(), raum="halle", tuer=True))
        self.assertIn("Wolfshund", sp.zug("RUNTER"))
        sp.st["hund"] = True
        self.assertIn("finster", sp.zug("RUNTER"))
        self.assertIn("verstehe", sp.zug("TANZE"))
        self.assertIn("Hagen", burg.Spiel(dict(burg.neues_spiel(), raum="turm")).zug("REDE MIT DEM MANN"))

    async def test_speichern_und_fortsetzen(self):
        s = FakeSitzung(["O", "HOCH"])
        await burg.run(s)
        self.assertIn("DIE BURG RABENSTEIN", s.text)
        self.assertEqual(s.speicher["spiel"]["raum"], "wehrgang")
        s2 = FakeSitzung(["WEITER", "RUNTER", "ENDE"], s.speicher)
        await burg.run(s2)
        self.assertIn("WILLKOMMEN ZURUECK", s2.text)
        self.assertEqual(s2.speicher["spiel"]["raum"], "hof")
        s3 = FakeSitzung(["NEU"], s2.speicher)
        await burg.run(s3)
        self.assertEqual(s3.speicher["spiel"]["raum"], "graben")
        self.assertEqual(s3.speicher["spiele"], 2)

    async def test_sieg_mit_bestwert(self):
        s = FakeSitzung(LOESUNG + ["ENDE"])
        await burg.run(s)
        self.assertEqual((s.speicher["siege"], s.speicher["bestwert"]), (1, 24))
        self.assertIn("NEUER BESTWERT", s.text)

    def test_text_passt_auf_fernschreiber(self):
        erlaubt = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -'():+,./=?\n")
        texte = [burg.INTRO, burg.HILFE] + [t for _, t in burg.RAEUME.values()]
        for t in texte:
            n = P.normalize(t)
            self.assertFalse(set(n) - erlaubt, f"Zeichen {set(n) - erlaubt} in {t[:40]}")


class AnrufTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "a.db"))
        self.ex = Exchange(Config(bind="127.0.0.1", tns_port=0, vx_port=0, forward=False), self.store)
        await self.ex.start()
        self.dienste = Dienste(self.ex, self.store)

    async def asyncTearDown(self):
        await self.ex.stop()
        self.tmp.cleanup()

    def test_plugins_gefunden(self):
        self.assertTrue({"burg", "echo"} <= set(plugins()))

    async def anruf(self, number: str, zeilen: list[str], warte_auf: list[str]) -> str:
        d = self.store.dienst(number)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.ex.cfg.vx_port)
        line = telex.Line(reader, writer, server=False, answerback="44444 tester d")
        line.start()
        await line.send_version()
        await line.write(P.pkt_direct_dial(f"{d['ext_code']:02d}"))
        await telex.first_of(line.ready, line.closed, timeout=10)
        for zeile, erwartet in zip(zeilen, warte_auf):
            for _ in range(150):
                if erwartet in line.rx_text:
                    break
                await asyncio.sleep(0.1)
            await line.send_text(zeile + "\r\n")
        await asyncio.sleep(1.5)
        await line.close()
        await asyncio.sleep(0.3)
        return line.rx_text

    async def test_echo_anruf(self):
        self.store.dienst_save({"number": "701", "name": "Echo", "plugin": "echo"}, set(self.dienste.plugins))
        text = await self.anruf("701", ["HALLO WELT", "ENDE"], ["ENDE MIT ENDE", "HALLO WELT"])
        self.assertIn("ECHO-TEST", text)
        self.assertIn("HALLO WELT", text.split("ENDE MIT ENDE", 1)[1])
        self.assertEqual(self.store.daten_get("echo", "44444")["anrufe"], 1)
        ev = self.store.events(kind="dienst")
        self.assertIn("echo: von 44444", ev[0]["detail"])

    async def test_burg_anruf_mit_spielstand(self):
        self.store.dienst_save({"number": "702", "name": "Burg", "plugin": "burg", "kennung": "702 burg d"},
                               set(self.dienste.plugins))
        text = await self.anruf("702", ["O", "ENDE"], ["VOR DEM BURGTOR", "AM MAUERFUSS"])
        self.assertIn("DIE BURG RABENSTEIN", text)
        self.assertIn("AM MAUERFUSS", text)
        self.assertIn("702 BURG D", text)
        self.assertEqual(self.store.daten_get("burg", "44444")["spiel"]["raum"], "mauer")

    async def test_dienst_im_verzeichnis(self):
        self.ex.cfg.public_host = "telex.example.org"
        d = self.store.dienst_save({"number": "70200", "name": "Burg", "plugin": "burg"}, set(self.dienste.plugins))
        rec, _ = await self.ex.lookup("70200")
        self.assertEqual(P.decode_extension(rec["extension"]), f"{d['ext_code']:02d}")
        with self.assertRaises(ValueError):
            self.store.dienst_save({"number": "703", "name": "X", "plugin": "gibtsnicht"}, set(self.dienste.plugins))


if __name__ == "__main__":
    unittest.main()
