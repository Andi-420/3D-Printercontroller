# 3D-Printercontroller

Status-Monitor für Bambu-Lab-Drucker (entwickelt für den **P2S**). Er zeigt live alles an,
was der Drucker über das lokale Netzwerk meldet.

**Nur lesen:** Der Monitor sendet keine Steuerbefehle. Er funktioniert deshalb auch im normalen
Cloud-Modus, und die **Bambu Handy App bleibt voll nutzbar**. LAN-Only- oder Developer-Mode
sind nicht nötig.


## Was angezeigt wird

- **Übersicht:** Druckzustand, Fortschritt, Schicht, Restzeit, Fertig-Uhrzeit, aktuelle Phase,
  Temperaturen von Düse, Bett und Kammer (Ist und Soll), Lüfter, Geschwindigkeitsstufe, WLAN-Signal,
  Düse, Licht, Kamera, Firmware, AMS mit Farben, Filamenttyp, Restmenge und Luftfeuchte sowie HMS-Fehlercodes
- **Alle Rohdaten:** jedes einzelne Feld, das der Drucker sendet, durchsuchbar, mit Erklärung
  (soweit bekannt) und gelber Markierung, sobald sich ein Wert ändert.
  Hier siehst du, was dein P2S **zusätzlich** alles verrät.
- **Download** des kompletten Zustands als JSON und optional ein **Mitschnitt** aller Rohnachrichten
- **Anonymisiert herunterladen:** ersetzt Seriennummern, IP-Adressen, URLs, Cloud-IDs und Dateinamen
  und ist damit zum Teilen gedacht. Messwerte, Filamentdaten und Firmware-Versionen bleiben erhalten.
  Der Mitschnitt per `--log` wird **nicht** anonymisiert.

## Einrichtung

Voraussetzung: Python 3.10 oder neuer und ein Rechner im selben Netzwerk wie der Drucker.
Unter Windows ggf. `py` statt `python3` verwenden, aber für Installation und Start immer denselben Befehl.

1. Abhängigkeit installieren:
   ```bash
   python3 -m pip install -r requirements.txt
   ```
2. Am Drucker-Display ablesen:
   - **IP-Adresse** und **Access Code**: Einstellungen → WLAN
   - **Seriennummer**: Einstellungen → Gerät (steht auch in Bambu Studio unter „Gerät“)
3. `.env.example` nach `.env` kopieren und die drei Werte eintragen.
   Die `.env` wird nicht ins Git-Repo übernommen.

## Starten

```bash
python3 monitor.py
```

Danach <http://localhost:8080> im Browser öffnen.

Ohne Drucker, mit Beispieldaten (ein echter, anonymisierter P2S-Status aus `demo/p2s_beispiel.json`):

```bash
python3 monitor.py --demo
```

Alle Rohnachrichten zusätzlich in eine Datei schreiben (eine JSON-Zeile pro Nachricht):

```bash
python3 monitor.py --log mitschnitt.jsonl
```

Vom Handy im Heimnetz aufrufen: in der `.env` `WEB_HOST=0.0.0.0` setzen und dann
`http://<IP-des-Rechners>:8080` öffnen. Die Seite ist ohne Passwort für alle im Heimnetz sichtbar,
das ist aber unkritisch, da sie nur liest und den Access Code nicht anzeigt.

Alle Optionen: `python3 monitor.py --help`

## Wie es funktioniert

Der Drucker betreibt einen MQTT-Server (TLS, Port 8883, Benutzer `bblp`, Passwort = Access Code).
Der Monitor abonniert `device/<SERIENNUMMER>/report`. Der Drucker schickt meist nur geänderte Felder;
der Monitor führt sie zu einem Gesamtzustand zusammen.

Einzige Anfragen an den Drucker (beide nur lesend):
- `pushall` beim Verbinden und über den Button. Damit kommt einmal der vollständige Status,
  höchstens einmal pro Minute, um den Drucker nicht zu belasten.
- `get_version` beim Verbinden, für die Firmware-Versionen.

## Fehlersuche

| Meldung | Ursache |
|---|---|
| „nicht erreichbar“ | IP falsch, Drucker aus oder in einem anderen Netz (z. B. Gäste-WLAN) |
| „Verbindung abgelehnt: Not authorized“ | Access Code falsch. Er ändert sich, wenn man ihn am Drucker neu erzeugt. |
| Verbunden, aber keine Daten | Seriennummer falsch |

Hinweis: Das Protokoll ist von Bambu Lab nicht offiziell dokumentiert. Feldnamen können sich
mit Firmware-Updates ändern. Unbekannte Felder werden trotzdem in den Rohdaten angezeigt.

## Tests

```bash
python3 -m unittest discover -s tests
```
