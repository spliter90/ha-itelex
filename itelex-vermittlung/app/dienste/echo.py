"""Testdienst: schreibt jede Zeile zurück. Gut, um eine Station oder Leitung zu prüfen."""

DIENST = {"id": "echo", "name": "Echo-Test",
          "beschreibung": "Schreibt jede getippte Zeile zurück. Zum Prüfen von Stationen. Ende mit ENDE."}


async def run(s) -> None:
    n = int(s.speicher.get("anrufe", 0)) + 1
    s.speicher["anrufe"] = n
    await s.schreibe(f"ECHO-TEST. {n}. ANRUF. JEDE ZEILE KOMMT ZURUECK, ENDE MIT ENDE.")
    while True:
        zeile = await s.lies()
        if zeile is None or zeile in ("ENDE", "NNNN"):
            break
        await s.schreibe(zeile)
    if s.verbunden:
        await s.schreibe("ECHO-TEST ENDE.")
