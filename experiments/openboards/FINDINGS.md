# OpenBoards: Ergebnisse der Tests (20.11.2026)

## Probe (Strecke Frankfurt Hbf → Mannheim Hbf)
- Vergangene Tafeln lassen sich abfragen, sogar 31 Tage zurück. Die angegebene 30-Tage-Grenze wird nicht hart durchgesetzt.
- Vergangene Abfahrten haben `timeType = REAL`. Die übrigen vergangenen Einträge (`PREVIEW`) waren ausnahmslos `cancelled = true`.
- `maxViaStops` immer setzen: ohne den Parameter kommen max. 7 Zwischenhalte, mit `100` bis zu 26.
- Das Zeitfenster filtert nach der **tatsächlichen** Zeit, nicht nach dem Fahrplan. Ein verspäteter Zug erscheint also in einem späteren Fenster.
- Pro Zugart kommen höchstens 50 Einträge. An großen Bahnhöfen ist schon ein 1-h-Fenster abgeschnitten (`meta.truncatedTransportTypes`). Dann muss man das Fenster teilen.

## Abgleich mit train_db.db (7 Tage, 20 Strecken, ca. 1600 Requests)
- **Verspätung:** Bei 97–100 % der gematchten Abfahrten ist der Wert minutengenau gleich, Median der Differenz 0. Es gibt einzelne Ausreißer von bis zu 24 min.
- **Zielfilter nach Bahnhof, nicht nach EVA-Nummer:** „Stuttgart Hbf“ (8000096) und „Stuttgart Hbf (tief)“ (8098096) haben dieselbe `stada` (6071). Ein Filter nur auf die EVA-Nummer übersieht die S-Bahn.
- **Lücke in den alten Daten:** Route 12 (Stuttgart Hbf → Esslingen) fehlen in der Datenbank rund 460 S-Bahn-Abfahrten pro Woche ab Hbf (tief). Die alte IRIS-Tafel für 8000096 enthielt sie nicht, OpenBoards schon.
- **Einträge nur in der DB, Stichprobe über 3 Tage:**
  - 40 von 52 sind Züge, deren Halt am Ziel ausgefallen ist (in OpenBoards `via[].cancelled = true`).
  - 2 sind komplett ausgefallene Abfahrten.
  - 10 sind Züge, die laut OpenBoards gar nicht bis zum Ziel fuhren (Teilausfall oder Umleitung). Die alte Datenbank hat sie trotzdem mit Verspätung erfasst.
- **Einträge nur in OpenBoards:** vor allem späte Abendzüge (23:xx) und einige Fahrten, die unter anderer Zugnummer zur gleichen Zeit laufen.

## Folgerungen für die Migration
- Ein Job, der periodisch die vergangenen Stunden je Startbahnhof abfragt, ersetzt das normale und das verzögerte Polling.
- Den Filter über `stada` vergleichen. Ausgefallene Ziel-Halte als eigenen Status speichern (Teilausfall), statt sie wegzufiltern.
- `journeyId` als eindeutigen Schlüssel verwenden. Tafeln pro Startbahnhof cachen, weil mehrere Strecken denselben Startbahnhof teilen.
