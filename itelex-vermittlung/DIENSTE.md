# Dienste-Plugins schreiben

Ein Dienst ist ein Python-Modul in `app/dienste/`. Es wird beim Start gefunden, erscheint in der
Oberfläche unter „Dienste“ und bekommt dort eine Telexnummer. Ruft jemand die Nummer an, startet
die Vermittlung `run()` mit einer Sitzung.

```python
# app/dienste/zahlenraten.py
import random

DIENST = {"id": "zahlenraten", "name": "Zahlenraten",
          "beschreibung": "Errate eine Zahl von 1 bis 100."}


async def run(s):
    ziel = random.randint(1, 100)
    await s.schreibe("ZAHLENRATEN. ICH DENKE AN EINE ZAHL VON 1 BIS 100.")
    for versuch in range(1, 11):
        zeile = await s.lies()
        if zeile is None or zeile == "ENDE":
            return
        if not zeile.isdigit():
            await s.schreibe("BITTE EINE ZAHL.")
            continue
        n = int(zeile)
        if n == ziel:
            s.speicher["siege"] = s.speicher.get("siege", 0) + 1
            await s.schreibe(f"RICHTIG, NACH {versuch} VERSUCHEN.")
            return
        await s.schreibe("ZU KLEIN." if n < ziel else "ZU GROSS.")
    await s.schreibe(f"LEIDER NICHT. ES WAR {ziel}.")
```

## Die Sitzung

| | |
|---|---|
| `await s.schreibe(text)` | Text ausgeben. Wird in Fernschreibschrift umgesetzt: Großbuchstaben, Umlaute umschrieben, 68 Zeichen je Zeile. `\n` beginnt eine neue Zeile. |
| `await s.lies(timeout=600)` | Nächste getippte Zeile, in Großbuchstaben. `None`, wenn aufgelegt wurde oder zu lange nichts kam. Kennung und Datumszeile des Anrufers werden überlesen. |
| `s.speicher` | Dict, bleibt je Plugin und Anrufer erhalten (JSON-fähige Werte). Wird nach `run()` gespeichert, auch wenn aufgelegt wurde. |
| `s.anrufer` / `s.anrufer_kennung` | Nummer und Kennung des Anrufers (`""`, wenn er sich nicht ausweist – dann wird nichts gespeichert). |
| `s.dienst` | Nummer, Name und Kennung, unter der das Plugin gerade angerufen wurde. |
| `s.verbunden` | Ob die Leitung noch steht. |

Wirft `schreibe()` die Ausnahme `Aufgelegt`, ist der Anrufer weg – einfach durchlaufen lassen.

## Tipps für den Fernschreiber

- 50 Baud sind gut 6 Zeichen pro Sekunde: Ein Absatz von 400 Zeichen braucht eine Minute. Kurz
  schreiben, lange Texte nur auf Wunsch.
- Erlaubte Zeichen: Buchstaben, Ziffern, Leerzeichen und `- ' ( ) + , . / : = ?`. Andere werden
  ersetzt (`!` wird `.`, `;` wird `,`).
- Eingaben kommen ohne Umlaute an (`SUED`, nicht `SÜD`).
- Plugins laufen im Server-Prozess: nichts Blockierendes aufrufen (kein `time.sleep`, keine
  langen Rechnungen), sonst stehen alle Anrufe.
- Testen ohne Fernschreiber: siehe `tests/test_dienste.py` (`FakeSitzung`).
