# i-Telex-Vermittlung ohne Home Assistant (Docker)

Derselbe Code wie das Add-on, als eigenständiger Container. Er enthält:

- **Teilnehmerverzeichnis** (Port 11811): beantwortet Nummernabfragen wie die öffentlichen
  i-Telex-Teilnehmerserver (Peer_query, ASCII-Abfrage `q<Nummer>`, Suche), nimmt IP-Meldungen
  dynamischer Stationen mit PIN an.
- **Vermittlung** (Port 49491): Stationen ohne Portweiterleitung melden sich ausgehend an
  (Centralex-Verfahren wie piTelex), Anrufe werden durchgereicht.
- **Telefonbuch**: eigene Teilnehmer und das öffentliche Verzeichnis, durchsuchbar.
- **Rufumleitung**: eigene Nummern dauerhaft oder bis zu einem Zeitpunkt auf eine andere Nummer.
- **Rundsenden**: Gruppen mit eigener Telexnummer, Verteilung an alle Mitglieder, Zustellbericht.
- **Abgleich** mit den anderen Teilnehmerservern (nur mit Server-PIN der i-Telex-Admins).
- **Verwaltung** im Browser (Port 8099) mit Passwort.

## Start

```sh
cd itelex-vermittlung
docker compose up -d --build
```

Oberfläche: `http://SERVER:8099`. Ohne `ITELEX_ADMIN_PASSWORD` erzeugt der Container beim ersten
Start ein Passwort und legt es in `data/admin-passwort.txt` ab.

Der Container nutzt das Host-Netz, damit er die echten Adressen der Anrufer sieht (die braucht er
für dynamische IPs und die Vermittlung). Die Daten liegen in `./data` (SQLite). Der Container läuft
als Benutzer mit der UID 1000, `./data` muss für ihn beschreibbar sein.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `ITELEX_PUBLIC_HOST` | leer | DynDNS-Name oder öffentliche IPv4 der Vermittlung. Nötig für Stationen „über Vermittlung“. |
| `ITELEX_PUBLIC_PORT` | 49491 | Port, den der Router auf 49491 weiterleitet. |
| `ITELEX_FORWARD` | true | Unbekannte Nummern im öffentlichen Netz nachschlagen. |
| `ITELEX_UPSTREAM` | tlnserv, tlnserv2, tlnserv3 | Öffentliche Teilnehmerserver (Komma-getrennt). |
| `ITELEX_SERVER_PIN` | leer | Server-PIN der i-Telex-Admins. Gesetzt = vollwertiger Teilnehmerserver mit Abgleich. |
| `ITELEX_SYNC_SERVERS` | = `ITELEX_UPSTREAM` | Partnerserver für den Abgleich (`host` oder `host:port`, Komma-getrennt). |
| `ITELEX_PHONEBOOK_HOURS` | 6 | Ohne Server-PIN: so oft wird das öffentliche Verzeichnis gespiegelt. |
| `ITELEX_ADMIN_PASSWORD` | erzeugt | Passwort der Oberfläche. Ändern meldet alle Sitzungen ab. |
| `ITELEX_LOG` | info | `debug`, `info` oder `warning`. |
| `ITELEX_WEB_PORT` / `ITELEX_TNS_PORT` / `ITELEX_VX_PORT` | 8099 / 11811 / 49491 | Ports im Container. |

## Rundsenden (Gruppen)

Unter **Rundsenden** legst du Gruppen mit eigener Telexnummer an (z. B. 70000 „Stammtisch“) und
trägst die Mitglieder ein – beliebige i-Telex-Nummern, auch Stationen dieser Vermittlung.

- **Per Telex:** Gruppennummer anrufen, Text schreiben, mit `NNNN` oder durch Auflegen beenden.
  Die Vermittlung stellt den Text jedem Mitglied einzeln zu (bei besetzt oder nicht erreichbar
  zwei weitere Versuche nach 1,5 und 4 Minuten) und schickt dem Absender danach einen
  **Zustellbericht** per Telex.
- **Aus der Oberfläche:** Knopf „Senden“ bei der Gruppe.
- **Wer senden darf**, stellst du je Gruppe ein: nur Mitglieder, zusätzlich freigegebene Nummern,
  oder alle. Erkannt wird der Absender an der Nummer in seiner Kennung.
- **Selbst an- und abmelden:** Ist es erlaubt, schickt ein Teilnehmer einfach `ANMELDEN` oder
  `ABMELDEN` an die Gruppennummer.
- Die Gruppennummer ist wie eine Station „über Vermittlung“ erreichbar, braucht also die
  **öffentliche Adresse** und die Portweiterleitung von 49491. Mit Abgleich steht sie im ganzen Netz.
- Der Verlauf zeigt je Rundsendung, wer sie bekommen hat (90 Tage).

## Router

Auf dem Anschluss mit öffentlicher IPv4 **11811/TCP** und **49491/TCP** auf den Server weiterleiten
(bei doppeltem NAT in beiden Routern). Port 8099 nicht ins Internet freigeben.

## Abgleich mit dem i-Telex-Netz

Ohne Server-PIN spiegelt die Vermittlung das öffentliche Verzeichnis über die Suche der öffentlichen
Server (alle `ITELEX_PHONEBOOK_HOURS` Stunden). Es dient als Telefonbuch und als Ersatz, wenn die
öffentlichen Server nicht erreichbar sind. Eigene Teilnehmer kennen dann nur Stationen, die diese
Vermittlung als Teilnehmerserver eingetragen haben.

Mit Server-PIN verhält sich die Vermittlung wie ein offizieller Teilnehmerserver:

- täglich Gesamtabfrage (Sync_FullQuery) bei jedem Partnerserver,
- eigene Teilnehmer und Änderungen werden per Sync_Login an die Partner gesendet (kurz nach einer
  Änderung, spätestens stündlich), gelöschte als „gelöscht“,
- Partner dürfen mit derselben PIN abfragen und senden,
- dynamische Stationen aus dem ganzen Netz können ihre IP hier melden.

Die PIN gibt es nur in Absprache mit den i-Telex-Admins. Mit falscher PIN trennen die Partner die
Verbindung sofort; die Oberfläche zeigt das unter „Abgleich“.

## Aktualisieren

```sh
git pull
docker compose up -d --build
```

## Tests

```sh
python -m unittest discover -s tests -v     # braucht aiohttp
```
