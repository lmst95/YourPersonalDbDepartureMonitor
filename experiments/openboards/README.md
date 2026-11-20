# OpenBoards-Experimente

Testcalls gegen die neue **OpenBoards-API**, den Nachfolger der Timetables/IRIS-API, und ein Abgleich mit den bisher gesammelten Daten.
Der produktive Code (`db_live_connections.py`, `db_live_api.py`) bleibt unverändert.

## Voraussetzungen

1. Im [DB API Marketplace](https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards) die bestehende App für den Plan **OpenBoards → Free** abonnieren.
2. `DB_CLIENT_ID` / `DB_API_KEY` wie gehabt in `.env` im Repo-Root eintragen.

## 1. Testcalls

```bash
python experiments/openboards/openboards_client.py probe --origin 8000105 --dest 8000191
```

Der Probe-Lauf prüft:
- ob Abfragen funktionieren: aktuelle Abfahrten, gestern, vor 29 Tagen und vor 31 Tagen (dort wird ein Fehler oder eine leere Antwort erwartet)
- die Verteilung von `timeType`: `SCHEDULE`, `PREVIEW` oder `REAL` (`REAL` = tatsächlich gefahrene Zeit)
- ob `maxViaStops` alle Halte liefert, die der Zielfilter braucht
- die Ankunftstafel am Zielbahnhof

Eine einzelne Tafel abfragen:

```bash
python experiments/openboards/openboards_client.py board --eva 8000105 --start 2026-11-18T07:00 --hours 2 --dest 8000191
```

## 2. Abgleich mit der bestehenden Datenbank

```bash
python experiments/openboards/compare_with_db.py --db ./train_db.db --days 7 --csv experiments/openboards/out/compare.csv
```

Die Datenbank wird nur gelesen. Pro Route gibt das Skript aus:
- die Zahl der gematchten Abfahrten. Gematcht wird über Zugnummer und geplante Abfahrt.
- welche Abfahrten nur in der Datenbank oder nur in OpenBoards vorkommen. Ein eigener Zähler zeigt die Fälle, in denen der Zug zwar am Start vorhanden ist, der Zielfilter aber nicht greift.
- die Verteilung der Differenz der Verspätung (OpenBoards minus Datenbank) und Abweichungen bei Ausfällen.

Rohantworten und CSV-Dateien landen in `experiments/openboards/out/`. Das Verzeichnis ist von git ausgenommen.
