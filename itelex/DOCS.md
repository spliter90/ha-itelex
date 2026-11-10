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

### Ohne Portweiterleitung: über eine Vermittlung

Hat der Anschluss keine öffentliche IPv4 (DS-Lite, Mobilfunk) oder soll keine Portweiterleitung
eingerichtet werden, meldet sich die Station ausgehend an einer Vermittlung an, z. B. an der
eigenen **i-Telex-Vermittlung** aus diesem Repository. Dazu unter **Vermittlung** deren Adresse
und unter **PIN** die dort angezeigte PIN eintragen. Eingehende Anrufe kommen dann durch die
Vermittlung, die Sperre per IP greift dabei nicht, die Sperre per Kennung schon.

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
- **Offene Leitung (live):** Nach dem Senden wartet die Leitung kurz auf eine Antwort (**Nach dem Senden
  auf Antwort warten**, Standard 15 Sekunden) und legt dann auf. So läuft die Maschine drüben nicht
  minutenlang leer, und der Anschluss ist schnell wieder frei. Fängt die Gegenstelle an zu tippen,
  bleibt die Leitung offen, bis **Auflegen im Gespräch** lang Ruhe ist (Standard 2 Minuten, jedes
  Zeichen setzt die Zeit zurück). Was die Gegenstelle tippt, erscheint Zeichen für Zeichen im Chat.
  Solange die Leitung offen ist, geht alles, was du schreibst, direkt in dieselbe Verbindung, ohne
  neuen Anruf. Schreiben, die Tasten und „Länger warten“ schalten ebenfalls auf die Gesprächszeit um.
- **Anrufen ohne Text:** Mit **Anrufen** (im Chat, bei „Neues Telex“ oder im Telefonbuch) baut die
  Station nur die Verbindung auf und tauscht die Kennungen aus, wie am echten Fernschreiber. Danach
  schreibst du live in die offene Leitung, und die Antworten der Gegenstelle erscheinen sofort.
- **Menüdienste:** In der offenen Leitung wird jede Eingabe so gesendet, wie man sie am Fernschreiber
  tippt: ohne Leerzeile davor, ohne Anfangs- und Endzeile und mit WR ZL am Ende. Die Felder „Anfang“
  und „Ende“ sind deshalb ausgeblendet, solange die Leitung offen ist. Enter schickt die Zeile ab, Umschalt+Enter
  macht eine neue Zeile. Für Dienste, die „mit ZL bestätigen“ verlangen, gibt es außerdem die Tasten
  **ZL** (Zeilenvorschub), **WR** (Wagenrücklauf), **WR ZL** und **Klingel**.
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
- **Anrufbeantworter:** In den Einstellungen (Zahnrad) einschaltbar, ebenso über den Schalter
  `switch.telex_anrufbeantworter` oder per Assist. Ist er an, bekommt jeder Anrufer nach der
  Kennungsabfrage eine Ansage gedruckt (z. B. „zur Zeit nicht besetzt, bitte Nachricht hinterlassen“).
  Was er danach tippt, wird gespeichert und im Chat mit 📼 markiert. Nach einstellbarer Ruhe
  (Standard 1 Minute) druckt er den Schlusstext und legt auf. Mit „Automatisch aus am“ schaltet er
  sich zum Rückkehrzeitpunkt selbst ab, der Zeitpunkt steht als `{zurueck}` in der Ansage.
- **Grafiken:** Der Knopf **Grafik** im Chat fügt Bilder aus Fernschreibzeichen ein. Alles besteht nur
  aus Zeichen, die ein Fernschreiber drucken kann (A–Z, 0–9, `- ' : ( ) + = / ? , .`), höchstens
  68 Zeichen breit. Zur Auswahl stehen fertige **Bilder** und **Linien**, ein **Schriftzug**-Generator
  (Blockschrift, Rahmen, Banner längs über das Papier), ein **Foto**-Umwandler (das Foto bleibt im
  Browser) und **eigene** gespeicherte Grafiken. Zu jeder Grafik steht die Druckzeit bei 50 Baud.
  In Automationen und per Assist lässt sich eine Grafik mit `{grafik:name}` im Text mitschicken,
  z. B. `{grafik:herz}`, `{grafik:tannenbaum}` oder der Name einer eigenen Grafik.
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
| `binary_sensor.itelex_anrufbeantworter` | Anrufbeantworter an/aus, Attribute `ansage`, `automatisch_aus` |
| `switch.telex_anrufbeantworter` | Schalter dafür (aus dem Paket `homeassistant/packages/itelex.yaml`) |

Events: `itelex_nachricht` (neues Telex, mit `anrufbeantworter: true`, wenn aufgezeichnet) und `itelex_gesendet` (Versand abgeschlossen, mit `status`).

Für Sprachbefehle die Dateien aus dem Ordner `homeassistant/` des Repositorys kopieren:

- `packages/itelex.yaml` nach `/config/packages/` (REST-Befehl, Intents, Benachrichtigungen)
- `custom_sentences/de/itelex.yaml` nach `/config/custom_sentences/de/`

Danach Home Assistant neu starten. Beispiele:

- „Sende Telex an 11150 Text Hallo aus Frankfurt“
- „Schick ein Telex an Fablab Nachricht Gruß von Chris“ (Name aus dem Telefonbuch)
- „Lies mir das letzte Telex vor“
- „Habe ich neue Telexe?“
- „Schalte den Telex-Anrufbeantworter ein“ / „Anrufbeantworter aus“
- „Ist der Anrufbeantworter an?“

## Hinweis

Hinter den meisten Nummern stehen echte Maschinen mit Papier. Bitte nicht automatisiert an fremde
Teilnehmer senden und zum Testen Dienstnummern verwenden.
