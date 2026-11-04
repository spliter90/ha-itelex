# i-Telex für Home Assistant

Zwei Home-Assistant-Add-ons für das i-Telex-Netz:

- **i-Telex** (`itelex/`): eine Telex-Station ohne Fernschreiber. Senden und Empfangen mit
  Chat-Oberfläche, offener Leitung mit Live-Antworten, Anrufliste, Sperrliste, Telefonbuch,
  Grußzeilen, Drucken und Sprachsteuerung über Assist.
- **i-Telex-Vermittlung** (`itelex-vermittlung/`): eigenes Teilnehmerverzeichnis und Vermittlung.
  Stationen ohne Portweiterleitung melden sich dort an, unbekannte Nummern werden im öffentlichen
  Netz nachgeschlagen.

## Installation

*Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories* und
`https://github.com/spliter90/ha-itelex` eintragen. Danach das gewünschte Add-on installieren.

Einrichtung: [itelex/DOCS.md](itelex/DOCS.md) und [itelex-vermittlung/DOCS.md](itelex-vermittlung/DOCS.md).

## Aufbau

```
itelex/                    Station
  app/protocol.py          i-Telex-Paketformat und Baudot (ITA2, Bitfolge wie piTelex)
  app/station.py           Senden, Empfangen, offene Leitung, Anmeldung an Vermittlung
  app/tns.py               Abfrage und IP-Meldung beim Teilnehmerverzeichnis
itelex-vermittlung/        Vermittlung
  app/exchange.py          Teilnehmerverzeichnis (11811) und Vermittlung (49491)
homeassistant/             Pakete für Assist und Benachrichtigungen
```

Protokolldetails sind gegen [piTelex](https://github.com/fablab-wue/piTelex) geprüft.
