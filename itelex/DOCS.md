# i-Telex für Home Assistant

Sendet und empfängt Fernschreiben im [i-Telex](https://www.i-telex.net/)-Netz, gleichzeitig und ohne
echten Fernschreiber. In der Seitenleiste erscheint **Telex** mit Chat, Anrufliste und Sperrliste.

## Einrichtung

1. **Kennung** eintragen, z. B. `12345 chris d` (Nummer, Kurzname, Länderkennung; max. 20 Zeichen).
   Sie wird gesendet, wenn eine Gegenstelle „Wer da?“ fragt.
2. **Nur senden:** Das funktioniert sofort. Die Zielnummer wird im Teilnehmerverzeichnis (TNS) aufgelöst.
3. **Empfangen:**
   - Port 134/TCP im Router auf Home Assistant weiterleiten (Host-Port unter *Netzwerk* änderbar).
   - i-Telex kann kein IPv6. Die Weiterleitung muss auf einem Anschluss mit echter öffentlicher IPv4
     liegen (kein DS-Lite). Beispiel MikroTik:
     `/ip firewall nat add chain=dstnat in-interface=WAN-MIT-IPV4 protocol=tcp dst-port=134 action=dst-nat to-addresses=HA-IP to-ports=134`
   - Bei den i-Telex-Admins eine Nummer eintragen lassen und hier unter **Eigene Telexnummer** eintragen.
     Zwei Wege, je nach Anschluss:
     - **Hostname (empfohlen):** Eintrag mit DynDNS-Namen, z. B. dem MikroTik-eigenen
       `…sn.mynetname.net` (IP → Cloud). Dann muss das Add-on nichts melden.
     - **Dynamische IP:** Die Admins legen den Eintrag als dynamisch an und geben dir eine PIN.
       **Dynamische IP melden** einschalten, PIN und **Öffentlichen Port** eintragen. Das Add-on meldet
       die IP beim Start und danach stündlich, wie i-Telex und piTelex.
   - **Zwei Internetzugänge:** Der Teilnehmerserver übernimmt die IP, von der die Meldung kommt.
     Verbindungen zu den Teilnehmerservern (Port 11811) müssen deshalb über den Zugang mit der
     Portweiterleitung laufen (Routing-Regel im MikroTik).

## Protokoll und Fehlersuche

Das Add-on schreibt Verbindungen, Abweisungen und Fehler ins Add-on-Protokoll (Add-on → Reiter
„Protokoll“). Mit **Protokollstufe** `debug` wird zusätzlich jedes i-Telex-Paket mitgeschrieben
(ohne die sekündlichen Bestätigungen). Dazu kommt die Anrufliste in der Oberfläche.

Während einer Verbindung sendet das Add-on jede Sekunde eine Bestätigung an die Gegenstelle, wie
i-Telex und piTelex. Das hält die Leitung offen. Ein dauerhafter „Ping“ ins Netz ist darüber hinaus
nicht nötig.

## Was das Add-on tut

- **Senden:** Text wird in Großschrift und 68 Zeichen pro Zeile umgesetzt. Ablauf: „Wer da?“,
  eigene Kennung, Text, erneut „Wer da?“ als Zustellbestätigung. Der Versand richtet sich nach den
  Druckbestätigungen der Gegenstelle, damit echte Maschinen mit 50 Baud mitkommen.
  Bei „besetzt“ oder „nicht erreichbar“ wird zweimal wiederholt (nach 1,5 und 4 Minuten).
- **Offene Leitung (live):** Nach dem Senden bleibt die Verbindung offen (Standard 5 Minuten Ruhe, in der Add-on-Konfiguration
  bis 30 Minuten einstellbar; jedes getippte Zeichen und der Knopf „Länger warten“ setzen die Zeit zurück).
  Was die Gegenstelle tippt, erscheint Zeichen für Zeichen im Chat. Solange die Leitung offen ist,
  geht alles, was du schreibst, direkt in dieselbe Verbindung, ohne neuen Anruf. Über dem Eingabefeld
  stehen „Wer da?“ und „Auflegen“.
- **Empfangen:** Eingehende Anrufe bleiben offen, bis der Anrufer auflegt (spätestens nach 5 Minuten
  Ruhe). Der Text erscheint live, und du kannst sofort antworten. Der Absender wird über seine Kennung
  erkannt.
- **Anrufblock:** i-Telex überträgt keine Rufnummer. Gesperrte Nummern werden über ihre IP aus dem
  TNS (stündlich aktualisiert) und über die Kennung erkannt. Gesperrte Anrufer bekommen „besetzt“.
  Mit **Annahme aus** werden alle eingehenden Anrufe abgewiesen.
- **Telefonbuch:** Nummern mit Namen speichern (Name lässt sich per „Prüfen“ aus dem
  Teilnehmerverzeichnis übernehmen). Namen erscheinen im Chat und in der Anrufliste, beim Schreiben
  kann statt der Nummer der Name eingegeben werden, auch per Assist.
- **Grußzeilen:** Anfangs- und Endzeile als Standard in den Einstellungen (Zahnrad), dazu eine
  Begrüßung für Anrufer. Im Chat stehen „Anfang“ und „Ende“ direkt über und unter dem Textfeld, jeweils
  einzeln abschaltbar und für die einzelne Nachricht änderbar, mit Vorschau. Platzhalter: `{datum}`,
  `{zeit}`, `{name}` (aus dem Telefonbuch), `{nummer}`, `{kennung}` (eigene).
  Ablauf beim Senden: Wer da? → eigene Kennung → Anfangszeile → Text → Endzeile → Wer da?
- **Speicher:** SQLite unter `/data`. Nachrichten und Anrufe werden nach 90 Tagen gelöscht; in den
  Einstellungen (Zahnrad) wählbar: 30, 60, 90, 180 oder 365 Tage.
- **Verläufe speichern:** Im Chat-Menü „Behalten“ nimmt einen Verlauf von der automatischen Löschung
  aus (Stecknadel in der Liste). „Als Datei speichern“ lädt ihn als Textdatei herunter.
- **Drucken:** Knopf „Drucken“ im Chat öffnet den Druckdialog mit dem Verlauf im Fernschreib-Stil.
  Falls das in der Companion-App nicht geht: Verlauf als Datei speichern und von dort drucken.

## Home Assistant und Assist

Das Add-on legt automatisch an:

| Entität | Inhalt |
|---|---|
| `sensor.itelex_ungelesen` | Anzahl ungelesener Telexe |
| `sensor.itelex_letzte_nachricht` | Letztes empfangenes Telex, Attribute `absender`, `kennung`, `text`, `vorlesen` |
| `binary_sensor.itelex_verbindung` | `on` während einer Verbindung |

Events: `itelex_nachricht` (neues Telex) und `itelex_gesendet` (Versand abgeschlossen, mit `status`).

Für Sprachbefehle die Dateien aus dem Ordner `homeassistant/` des Repositorys kopieren:

- `packages/itelex.yaml` nach `/config/packages/` (REST-Befehl, Intents, Benachrichtigungen)
- `custom_sentences/de/itelex.yaml` nach `/config/custom_sentences/de/`

Danach Home Assistant neu starten. Beispiele:

- „Sende Telex an 11150 Text Hallo aus Frankfurt“
- „Schick ein Telex an Fablab Nachricht Gruß von Chris“ (Name aus dem Telefonbuch)
- „Lies mir das letzte Telex vor“
- „Habe ich neue Telexe?“

## Hinweis

Hinter den meisten Nummern stehen echte Maschinen mit Papier. Bitte nicht automatisiert an fremde
Teilnehmer senden und zum Testen Dienstnummern verwenden.
