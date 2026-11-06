"""Die Burg Rabenstein – ein Textabenteuer für den Fernschreiber.

Der Raubgraf von Rabenstein hat das Siegel des Königs gestohlen. Als Ritter schleichst du dich in
die Burg, holst das Siegel und entkommst durch das Tor, bevor die Wachen dich fassen.

Lösungsweg (für Tests und für den Verwalter):
  O, HOCH, RUNTER (Hof), O, UNTERSUCHE HEU, NIMM SCHLUESSEL, W, W, NIMM FACKEL,
  DREHE KURBEL (Gitter auf), O, OEFFNE TUER, N, W, NIMM SCHINKEN, O, GIB SCHINKEN, RUNTER, O,
  NIMM SIEGEL, W, HOCH, S, S  -> gewonnen (24 Züge).
Der Spielstand wird nach jedem Zug je Anrufer gespeichert.
"""

from __future__ import annotations

import re

DIENST = {"id": "burg", "name": "Die Burg Rabenstein",
          "beschreibung": "Textabenteuer: Als Ritter holst du das gestohlene Siegel des Königs aus der Burg "
                          "des Raubgrafen. Spielstand bleibt je Anrufer erhalten."}

ALARM_ZUEGE = 8

INTRO = (
    "DIE BURG RABENSTEIN\n"
    "Ein Abenteuer in Textform\n\n"
    "Du bist ein Ritter des Königs. Der Raubgraf von Rabenstein hat das königliche Siegel "
    "gestohlen - ohne es ist kein Brief des Königs gültig. Allein kannst du die Burg nicht "
    "erstürmen. Also musst du hinein, ohne dass man dich bemerkt, das Siegel holen und "
    "lebend wieder hinaus.\n\n"
    "Es ist Nacht. Nebel liegt über dem Burggraben."
)

HILFE = (
    "Befehle: N S O W HOCH RUNTER (oder GEHE NORD ...), SCHAU, UNTERSUCHE ..., NIMM ..., "
    "BENUTZE ..., GIB ..., REDE, INVENTAR, HILFE, NEU (von vorn), ENDE (speichert).\n"
    "Kurze Eingaben genügen, z. B. NIMM FACKEL."
)

# --- Welt -------------------------------------------------------------------------------
RAEUME = {
    "graben": ("Vor dem Burgtor",
               "Du stehst am Graben vor dem Tor der Burg. Ein schweres Fallgitter versperrt den Weg "
               "nach Norden. Nach Osten führt ein Pfad an der Mauer entlang."),
    "mauer": ("Am Mauerfuß",
              "Die Burgmauer ragt hoch über dir auf. Dichter, alter Efeu rankt an ihr empor. "
              "Nach Westen geht es zurück zum Tor."),
    "wehrgang": ("Auf dem Wehrgang",
                 "Ein schmaler Gang auf der Mauerkrone. Eine Wache schläft, auf ihre Hellebarde "
                 "gestützt, und schnarcht leise. Im Osten steht ein Turm, eine Treppe führt hinunter "
                 "in den Hof. Der Efeu reicht bis hier herauf."),
    "turm": ("Im Turm",
             "Eine kalte Turmstube. In einem Eisenkäfig kauert ein abgemagerter Mann. "
             "Der Weg zurück führt nach Westen."),
    "hof": ("Im Burghof",
            "Der Hof liegt still im Mondlicht. Im Norden das Haupthaus mit einer schweren Eichentür, "
            "im Osten der Stall, im Westen die Wachstube. Im Süden liegt das Fallgitter. "
            "Eine Treppe führt hinauf auf den Wehrgang."),
    "stall": ("Im Stall",
              "Es riecht nach Pferd. Der schwarze Hengst des Grafen schnaubt in seiner Box. "
              "In einer Ecke liegt ein Haufen Heu. Der Ausgang ist im Westen."),
    "wachstube": ("In der Wachstube",
                  "Ein grober Tisch mit Würfeln und leeren Bechern. An der Wand ist eine große "
                  "Kurbel mit einer Kette, die durch die Mauer zum Tor läuft. Der Hof liegt im Osten."),
    "halle": ("In der großen Halle",
              "Lange Tafeln, kalte Asche im Kamin. Im Westen geht es in die Küche, im Süden in den Hof. "
              "Eine Treppe führt hinab in den Keller."),
    "kueche": ("In der Küche",
               "Kupferkessel und Messer. Die Küche ist im Osten mit der Halle verbunden."),
    "keller": ("Im Weinkeller",
               "Im Fackelschein siehst du Fässer an den Wänden. Im Osten ist eine niedrige, "
               "eisenbeschlagene Tür angelehnt. Die Treppe führt hinauf in die Halle."),
    "schatzkammer": ("In der Schatzkammer",
                     "Truhen, Pokale, Wandteppiche - die Beute vieler Raubzüge. Der Weg zurück "
                     "führt nach Westen."),
}

WEGE = {
    "graben": {"n": "hof", "o": "mauer"},
    "mauer": {"w": "graben", "hoch": "wehrgang"},
    "wehrgang": {"o": "turm", "runter": "hof"},
    "turm": {"w": "wehrgang"},
    "hof": {"n": "halle", "o": "stall", "w": "wachstube", "hoch": "wehrgang", "s": "graben"},
    "stall": {"w": "hof"},
    "wachstube": {"o": "hof"},
    "halle": {"w": "kueche", "s": "hof", "runter": "keller"},
    "kueche": {"o": "halle"},
    "keller": {"hoch": "halle", "o": "schatzkammer"},
    "schatzkammer": {"w": "keller"},
}

RICHTUNG = {"N": "n", "NORD": "n", "NORDEN": "n", "S": "s", "SUED": "s", "SUEDEN": "s",
            "O": "o", "OST": "o", "OSTEN": "o", "W": "w", "WEST": "w", "WESTEN": "w",
            "HOCH": "hoch", "RAUF": "hoch", "HINAUF": "hoch", "OBEN": "hoch", "H": "hoch",
            "RUNTER": "runter", "HINAB": "runter", "HINUNTER": "runter", "UNTEN": "runter", "R": "runter"}

# Gegenstände: Name, Synonyme, Ort zu Beginn (None = noch versteckt)
DINGE = {
    "schluessel": ("ein rostiger Schlüssel", {"SCHLUESSEL", "SCHLUSSEL"}),
    "fackel": ("eine brennende Fackel", {"FACKEL", "LICHT"}),
    "schinken": ("ein geräucherter Schinken", {"SCHINKEN", "FLEISCH", "WURST"}),
    "siegel": ("das Siegel des Königs", {"SIEGEL"}),
}
START_ORTE = {"fackel": "wachstube", "schinken": "kueche", "siegel": "schatzkammer"}

WOERTER = {  # feste Dinge in Räumen
    "KURBEL": "kurbel", "KETTE": "kurbel", "TUER": "tuer", "EICHENTUER": "tuer", "PFORTE": "tuer",
    "HUND": "hund", "WOLFSHUND": "hund", "HEU": "heu", "HEUHAUFEN": "heu", "MANN": "gefangener",
    "GEFANGENER": "gefangener", "GEFANGENEN": "gefangener", "HAGEN": "gefangener", "KAEFIG": "gefangener",
    "WACHE": "wache", "HELLEBARDE": "wache", "PFERD": "pferd", "HENGST": "pferd", "WUERFEL": "wuerfel",
    "EFEU": "efeu", "MAUER": "efeu", "GITTER": "gitter", "FALLGITTER": "gitter", "TOR": "gitter",
    "FAESSER": "faesser", "FASS": "faesser", "WEIN": "faesser", "TRUHEN": "truhen", "TRUHE": "truhen",
    "KAMIN": "kamin", "TISCH": "wuerfel",
}
FUELL = {"DEN", "DIE", "DAS", "DEM", "DER", "EIN", "EINE", "EINEN", "MIT", "AN", "AUF", "ZU", "ZUM",
         "IN", "INS", "NACH", "BITTE", "MAL"}


def neues_spiel() -> dict:
    return {"raum": "graben", "inv": [], "orte": dict(START_ORTE), "gitter": False, "tuer": False,
            "hund": False, "heu": False, "alarm": None, "zuege": 0, "besucht": ["graben"], "ende": ""}


class Spiel:
    def __init__(self, st: dict) -> None:
        self.st = st

    # --- Beschreibung -----------------------------------------------------------------------
    def dinge_hier(self) -> list[str]:
        return [d for d, ort in self.st["orte"].items() if ort == self.st["raum"]]

    def beschreibe(self, lang: bool = True) -> str:
        r = self.st["raum"]
        titel, text = RAEUME[r]
        teile = [titel.upper()]
        if lang:
            teile.append(text)
            if r == "graben" and self.st["gitter"]:
                teile.append("Das Fallgitter ist hochgezogen.")
            if r == "hof":
                teile.append("Die Eichentür steht offen." if self.st["tuer"] else "Die Eichentür ist verschlossen.")
                if self.st["gitter"]:
                    teile.append("Das Fallgitter ist oben - der Weg nach Süden ist frei.")
            if r == "halle":
                teile.append("Der Wolfshund liegt satt neben der Treppe und döst." if self.st["hund"]
                             else "Vor der Kellertreppe liegt ein riesiger Wolfshund und knurrt dich an.")
            if r == "wachstube" and self.st["gitter"]:
                teile.append("Die Kurbel ist ganz aufgedreht.")
        dinge = self.dinge_hier()
        if dinge:
            teile.append("Hier liegt: " + ", ".join(DINGE[d][0] for d in dinge) + ".")
        return "\n".join(teile)

    # --- Zug ---------------------------------------------------------------------------------
    def zug(self, eingabe: str) -> str:
        worte = [w for w in re.sub(r"[^A-Z ]", " ", eingabe.upper()).split() if w not in FUELL]
        if not worte:
            return "Wie bitte? (HILFE zeigt die Befehle)"
        verb, rest = worte[0], worte[1:]
        if verb in ("GEHE", "LAUF", "LAUFE", "RENNE", "GEH") and rest:
            verb, rest = rest[0], rest[1:]
        if verb in ("KLETTERE", "KLETTER", "STEIG", "STEIGE"):
            ziel = rest[0] if rest else ""
            if self.st["raum"] == "mauer":
                verb = "HOCH"
            elif self.st["raum"] == "wehrgang" and (ziel in ("RUNTER", "HINAB", "HINUNTER", "EFEU", "")):
                return self.efeu_runter()
            else:
                return "Hier gibt es nichts zum Klettern."
        if verb in RICHTUNG:
            return self.gehe(RICHTUNG[verb])
        obj = self.objekt(rest)
        if verb in ("SCHAU", "SIEH", "UMSEHEN", "L", "BLICK", "SCHAUE"):
            return self.beschreibe() if not rest else self.untersuche(obj)
        if verb in ("UNTERSUCHE", "U", "BETRACHTE", "DURCHSUCHE", "SUCHE", "LIES", "PRUEFE"):
            return self.untersuche(obj)
        if verb in ("NIMM", "HOL", "HOLE", "NEHME", "PACK", "PACKE"):
            return self.nimm(obj)
        if verb in ("GIB", "WIRF", "FUETTERE", "FUETTER", "LEG", "LEGE"):
            return self.gib(obj, rest)
        if verb in ("BENUTZE", "DREHE", "DREH", "ZIEH", "ZIEHE", "KURBLE", "BETAETIGE"):
            return self.benutze(obj)
        if verb in ("OEFFNE", "SCHLIESSE", "SCHLIESS", "AUFSCHLIESSEN"):
            return self.benutze(obj or "tuer")
        if verb in ("REDE", "SPRICH", "FRAGE", "RUFE", "SAG", "SAGE"):
            return self.rede(obj)
        if verb in ("KAEMPFE", "TOETE", "SCHLAG", "SCHLAGE", "ANGREIFEN", "GREIF"):
            return "Du bist ein Ritter des Königs, kein Meuchler. Und Lärm würde die ganze Burg wecken."
        if verb in ("WECKE", "WECK"):
            return "Keine gute Idee. Lass schlafende Wachen schlafen."
        if verb in ("INVENTAR", "I", "TASCHE", "BESITZ"):
            return self.inventar()
        if verb in ("WARTE", "WARTEN", "Z"):
            return "Du wartest einen Augenblick."
        return "Das verstehe ich nicht. (HILFE zeigt die Befehle)"

    def objekt(self, worte: list[str]) -> str:
        for w in worte:
            for d, (_, syn) in DINGE.items():
                if w in syn:
                    return d
            if w in WOERTER:
                return WOERTER[w]
        return ""

    def gehe(self, richtung: str) -> str:
        r = self.st["raum"]
        ziel = WEGE[r].get(richtung)
        if not ziel:
            return "Dort geht es nicht weiter."
        if (r, richtung) in (("graben", "n"), ("hof", "s")) and not self.st["gitter"]:
            return "Das Fallgitter ist unten. Zu schwer, um es mit bloßen Händen zu heben."
        if r == "hof" and richtung == "n" and not self.st["tuer"]:
            return "Die Eichentür ist verschlossen."
        if r == "halle" and richtung == "runter" and not self.st["hund"]:
            return "Der Wolfshund springt auf und fletscht die Zähne. Du weichst zurück."
        if r == "halle" and richtung == "runter" and "fackel" not in self.st["inv"]:
            return "Unten ist es stockfinster. Ohne Licht brichst du dir den Hals - du kehrst um."
        if r == "hof" and richtung == "hoch" and self.st["alarm"] is not None:
            return self.gefasst("Auf dem Wehrgang steht die Wache jetzt hellwach. Sie stellt dich mit der Hellebarde.")
        self.st["raum"] = ziel
        neu = ziel not in self.st["besucht"]
        if neu:
            self.st["besucht"].append(ziel)
        if ziel == "graben" and "siegel" in self.st["inv"]:
            return self.gewonnen()
        return self.beschreibe(lang=neu)

    def efeu_runter(self) -> str:
        if self.st["alarm"] is not None:
            return self.gefasst("Die Wache ist erwacht und packt dich, als du über die Brüstung steigst.")
        self.st["raum"] = "mauer"
        return "Vorsichtig kletterst du am Efeu hinab.\n" + self.beschreibe(lang=False)

    def untersuche(self, obj: str) -> str:
        r = self.st["raum"]
        if not obj:
            return self.beschreibe()
        if obj in DINGE:
            if obj in self.st["inv"] or self.st["orte"].get(obj) == r:
                return {"schluessel": "Alt und rostig, aber er passt bestimmt in ein großes Schloss.",
                        "fackel": "Sie brennt hell und ruhig.",
                        "schinken": "Er duftet herrlich. Nicht nur für Menschen.",
                        "siegel": "Das Siegel des Königs, schwer und golden. Deshalb bist du hier."}[obj]
            return "Das siehst du hier nicht."
        if obj == "heu" and r == "stall":
            if not self.st["heu"]:
                self.st["heu"] = True
                self.st["orte"]["schluessel"] = "stall"
                return "Du wühlst im Heu - und stößt auf einen rostigen Schlüssel."
            return "Nur noch Heu."
        texte = {
            ("kurbel", "wachstube"): "Eine schwere Kurbel. Die Kette läuft durch die Mauer zum Fallgitter.",
            ("tuer", "hof"): "Eichenholz mit Eisenbeschlägen und einem großen Schlüsselloch.",
            ("hund", "halle"): "Ein riesiger grauer Wolfshund. Er sieht hungrig aus.",
            ("gefangener", "turm"): "Ein alter Mann in Lumpen. Er sieht dich mit wachen Augen an.",
            ("wache", "wehrgang"): "Sie schläft tief. Ein Weinkrug liegt umgekippt neben ihr.",
            ("pferd", "stall"): "Ein prächtiger Rappe. Er beobachtet dich, bleibt aber ruhig.",
            ("wuerfel", "wachstube"): "Gezinkte Würfel. Typisch für die Leute des Grafen.",
            ("efeu", "mauer"): "Der Efeu ist dick und fest. Daran könnte man hinaufklettern.",
            ("efeu", "wehrgang"): "Der Efeu reicht bis zur Brüstung.",
            ("gitter", "graben"): "Eisenstäbe, dick wie dein Arm. Sie werden von innen hochgekurbelt.",
            ("gitter", "hof"): "Das Fallgitter wird über eine Kette aus der Wachstube bewegt.",
            ("faesser", "keller"): "Bester Wein - gestohlen, natürlich.",
            ("truhen", "schatzkammer"): "Gold und Silber. Du nimmst nur, was dem König gehört.",
            ("kamin", "halle"): "Kalte Asche.",
        }
        return texte.get((obj, r), "Daran fällt dir nichts Besonderes auf.")

    def nimm(self, obj: str) -> str:
        if not obj:
            return "Was willst du nehmen?"
        if obj in DINGE and self.st["orte"].get(obj) == self.st["raum"]:
            del self.st["orte"][obj]
            self.st["inv"].append(obj)
            if obj == "siegel":
                self.st["alarm"] = ALARM_ZUEGE
                return ("Du hebst das Siegel vom Samtkissen - und irgendwo klickt ein Mechanismus. "
                        "Oben beginnt eine Glocke zu läuten. Die Burg erwacht. Schnell hinaus.")
            return "Genommen: " + DINGE[obj][0] + "."
        if obj in self.st["inv"]:
            return "Das hast du schon."
        if obj in ("kurbel", "tuer", "gitter", "faesser", "truhen"):
            return "Das lässt sich nicht mitnehmen."
        if obj in ("hund", "pferd", "wache", "gefangener"):
            return "Das wäre keine gute Idee."
        return "Das siehst du hier nicht."

    def gib(self, obj: str, worte: list[str]) -> str:
        if self.st["raum"] != "halle" or self.st["hund"]:
            return "Hier will niemand etwas von dir." if obj != "schinken" else "Hier hat niemand Hunger."
        if obj in ("schinken", "hund") or "SCHINKEN" in worte:
            if "schinken" not in self.st["inv"]:
                return "Du hast nichts, was den Hund interessieren würde."
            self.st["inv"].remove("schinken")
            self.st["hund"] = True
            return "Du wirfst dem Hund den Schinken hin. Er schlingt ihn hinunter und legt sich zufrieden zur Seite."
        return "Der Hund ist daran nicht interessiert."

    def benutze(self, obj: str) -> str:
        r = self.st["raum"]
        if obj == "kurbel" and r == "wachstube":
            if self.st["gitter"]:
                return "Das Gitter ist schon oben."
            self.st["gitter"] = True
            return "Mit aller Kraft drehst du die Kurbel. Rasselnd hebt sich draußen das Fallgitter."
        if obj in ("schluessel", "tuer") and r == "hof":
            if self.st["tuer"]:
                return "Die Tür ist schon offen."
            if "schluessel" not in self.st["inv"]:
                return "Die Tür ist verschlossen. Du brauchst einen Schlüssel."
            self.st["tuer"] = True
            return "Der Schlüssel knirscht im Schloss - die Eichentür schwingt auf."
        if obj == "fackel":
            return "Die Fackel brennt schon." if "fackel" in self.st["inv"] else "Du hast keine Fackel."
        if obj == "schinken":
            return self.gib("schinken", [])
        return "Damit kannst du hier nichts anfangen."

    def rede(self, obj: str) -> str:
        r = self.st["raum"]
        if r == "turm":
            return ("Der Alte flüstert: Ich bin Hagen, einst Knappe hier. Höre: Der Schlüssel zum "
                    "Haupthaus liegt im Heu, wo der Graf ihn versteckt. Der Hund liebt Geräuchertes. "
                    "Und wenn du nimmst, was du suchst, läutet die Glocke - dann erwacht die Wache, und "
                    "nur das Tor bringt dich hinaus. Zieh das Gitter hoch, bevor du gehst.")
        if r == "halle":
            return "Der Hund knurrt nur."
        if r == "wehrgang":
            return "Psst. Die Wache schläft - lass es so."
        if r == "stall":
            return "Der Hengst schnaubt und schweigt."
        return "Niemand antwortet."

    def inventar(self) -> str:
        teile = ["Dein Schwert"] + [DINGE[d][0] for d in self.st["inv"]]
        return "Du trägst: " + ", ".join(teile) + "."

    # --- Ende ---------------------------------------------------------------------------------
    def nach_zug(self) -> str:
        """Nach jedem Zug: Alarm herunterzählen."""
        if self.st["alarm"] is None or self.st["ende"]:
            return ""
        self.st["alarm"] -= 1
        if self.st["alarm"] <= 0:
            return self.gefasst("Fackeln von allen Seiten - die Wachen des Grafen umstellen dich.")
        if self.st["alarm"] <= 3:
            return "Die Schritte der Wachen kommen näher."
        return "Die Glocke läutet. Rufe hallen durch die Burg."

    def gefasst(self, text: str) -> str:
        self.st["ende"] = "gefasst"
        return (text + "\n\nDU WURDEST GEFASST. Der Graf lässt dich in den Turm werfen - zu Hagen. "
                "Vielleicht beim nächsten Mal. (NEU fängt von vorn an)")

    def gewonnen(self) -> str:
        self.st["ende"] = "gewonnen"
        return ("Du rennst unter dem Fallgitter hindurch und verschwindest im Nebel. Hinter dir tobt "
                "der Graf auf der Mauer. Das Siegel des Königs ist in Sicherheit.\n\n"
                f"GEWONNEN. Züge: {self.st['zuege']}.")


async def run(s) -> None:
    sp = s.speicher
    st = sp.get("spiel")
    if st and not st.get("ende"):
        await s.schreibe(f"DIE BURG RABENSTEIN\nWillkommen zurück. Dein Spiel ist gespeichert ({st['zuege']} Züge). "
                         "WEITER oder NEU?")
        antwort = await s.lies()
        if antwort is None:
            return
        if antwort.startswith("NEU"):
            st = None
        else:
            sp["spiel"] = st
            await s.schreibe(Spiel(st).beschreibe())
    if not st or st.get("ende"):
        st = neues_spiel()
        sp["spiel"] = st
        sp["spiele"] = int(sp.get("spiele", 0)) + 1
        await s.schreibe(INTRO)
        await s.schreibe(HILFE)
        await s.schreibe("\n" + Spiel(st).beschreibe())
    spiel = Spiel(st)
    while True:
        eingabe = await s.lies()
        if eingabe is None:
            return                                         # aufgelegt: Stand bleibt gespeichert
        befehl = eingabe.split()[0] if eingabe.split() else ""
        if befehl in ("ENDE", "STOP", "NNNN", "TSCHUESS", "QUIT"):
            await s.schreibe("Dein Spiel ist gespeichert. Ruf wieder an, um weiterzuspielen. Lebewohl, Ritter.")
            return
        if befehl in ("HILFE", "?", "BEFEHLE"):
            await s.schreibe(HILFE)
            continue
        if befehl == "NEU":
            st = neues_spiel()
            sp["spiel"] = st
            sp["spiele"] = int(sp.get("spiele", 0)) + 1
            spiel = Spiel(st)
            await s.schreibe("Ein neues Spiel beginnt.\n\n" + spiel.beschreibe())
            continue
        if st["ende"]:
            await s.schreibe("Das Spiel ist vorbei. NEU fängt von vorn an, ENDE legt auf.")
            continue
        st["zuege"] += 1
        antwort = spiel.zug(eingabe)
        extra = spiel.nach_zug()
        await s.schreibe(antwort + ("\n" + extra if extra else ""))
        if st["ende"] == "gewonnen":
            sp["siege"] = int(sp.get("siege", 0)) + 1
            if not sp.get("bestwert") or st["zuege"] < sp["bestwert"]:
                sp["bestwert"] = st["zuege"]
                await s.schreibe(f"Neuer Bestwert: {st['zuege']} Züge.")
            await s.schreibe("Danke fürs Spielen. NEU fängt von vorn an, ENDE legt auf.")
