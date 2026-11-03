# i-Telex für Home Assistant

Home-Assistant-Add-on, das Fernschreiben im i-Telex-Netz sendet und empfängt: Chat-Oberfläche in der
Seitenleiste, Anrufliste, Sperrliste, 90-Tage-Speicher und Sprachsteuerung über Assist.

## Installation

**Als Repository:** *Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositories* und
`https://github.com/spliter90/ha-itelex` eintragen. Danach „i-Telex“ installieren.

**Lokal:** Ordner `itelex/` nach `/addons/itelex/` kopieren (Samba oder SSH), im Add-on-Store
*⋮ → Nach Updates suchen*, dann unter *Lokale Add-ons* installieren.

Einrichtung, Router und Assist: siehe [itelex/DOCS.md](itelex/DOCS.md).

## Aufbau

```
itelex/                 Add-on
  app/protocol.py       i-Telex-Paketformat und Baudot (ITA2)
  app/tns.py            Abfrage des Teilnehmerverzeichnisses
  app/station.py        Senden und Empfangen, Flusskontrolle, Kennung, Sperren
  app/store.py          SQLite (Nachrichten, Anrufe, Sperrliste)
  app/web.py, static/   API und Oberfläche (Ingress)
  app/hass.py           Sensoren und Events in Home Assistant
homeassistant/          Pakete für Assist und Benachrichtigungen
```

Protokolldetails wurden gegen [piTelex](https://github.com/fablab-wue/piTelex) abgeglichen.
