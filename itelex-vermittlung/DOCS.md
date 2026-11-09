# i-Telex-Vermittlung

Eigene kleine i-Telex-Zentrale für dich und deine Freunde. Sie besteht aus zwei Teilen:

- **Teilnehmerverzeichnis** (Port 11811): beantwortet Nummernabfragen wie die öffentlichen
  i-Telex-Teilnehmerserver. Eigene Einträge haben Vorrang, alle anderen Nummern werden im
  öffentlichen Verzeichnis nachgeschlagen (abschaltbar). Stationen mit wechselnder IP melden ihre
  Adresse mit PIN hier an.
- **Vermittlung** (Port 49491): Stationen ohne Portweiterleitung, z. B. hinter DS-Lite oder im
  Mobilfunk, melden sich ausgehend an und bleiben verbunden. Ruft jemand an, reicht die Vermittlung
  das Gespräch an sie durch. Das Verfahren ist das von piTelex („Centralex“), piTelex-Stationen
  können sich also ebenfalls anmelden.

In der Seitenleiste erscheint **Vermittlung** mit Teilnehmerliste, Online-Status, laufenden
Gesprächen und Protokoll.

## Einrichtung

1. **Öffentliche Adresse** eintragen: der Name oder die IPv4, unter der die Vermittlung aus dem
   Internet erreichbar ist, z. B. der DynDNS-Name des MikroTik (IP → Cloud). Nur im Heimnetz genügt
   die interne IP von Home Assistant.
2. Im Router **11811/TCP** und **49491/TCP** auf Home Assistant weiterleiten, auf dem Anschluss
   mit öffentlicher IPv4. Die fertigen MikroTik-Befehle stehen in der Oberfläche unter „Einrichtung“.
3. Unter **Teilnehmer** die Stationen eintragen und die Anbindung wählen:
   - **Direkt**: Station mit fester Adresse und eigener Portweiterleitung.
   - **Dynamische IP**: Station meldet ihre wechselnde IP mit PIN.
   - **Über Vermittlung**: Station ohne Portweiterleitung, meldet sich an der Vermittlung an.
4. Mit **Zugang** die PIN und die fertigen Einträge für das i-Telex-Add-on oder piTelex anzeigen.

## Station anbinden

Im **i-Telex-Add-on** der Station (Konfiguration):

```yaml
nummer: "33333"
tns_pin: 12345             # PIN aus „Zugang“
vermittlung: telex.example.org   # nur bei „Über Vermittlung“
vermittlung_port: 49491
tns_server:
  - telex.example.org      # eigene Vermittlung zuerst
  - tlnserv.teleprinter.net
```

Bei **piTelex** (`telex.json`) entsprechend `centralex`, `centralex_srv`, `centralex_port`,
`tns_dynip_number`, `tns_pin` und `tns_srv`. Die Oberfläche zeigt die passenden Zeilen an.

## Gut zu wissen

- Wer deine eigenen Teilnehmer anrufen will, muss die Vermittlung als Teilnehmerserver eintragen.
  Stationen, die nur die öffentlichen Server fragen, kennen deine Einträge nicht.
- Anrufer erreichen Stationen „über Vermittlung“ über eine Durchwahl (01–99), die das Verzeichnis
  automatisch mitliefert. Bis zu 99 solcher Stationen sind möglich.
- Gespräche werden unverändert durchgereicht. Die Vermittlung liest nicht mit und speichert keine
  Texte, nur wer wann wen erreicht hat (30 Tage).
- Home Assistant erhält den Sensor `sensor.itelex_vermittlung` (Anzahl angemeldeter Stationen).
