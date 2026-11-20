# Neues Design: Abfrage und Datenhaltung mit OpenBoards

Status: umgesetzt (v3.0.0). Grundlage sind die Testergebnisse in [experiments/openboards/FINDINGS.md](../experiments/openboards/FINDINGS.md).

## Ziele

1. **Live:** Für die Anzeige gibt es aktuelle Abfahrten mit Prognose, maximal etwa 10 min alt.
2. **Endgültig:** Einmal täglich werden die echten Ist-Zeiten und Ausfälle festgeschrieben. Erst diese Werte gehen in die Statistik ein.
3. **Lückenlos:** Wenn der Server ausfällt oder eine Strecke neu dazukommt, werden die Daten automatisch nachgeholt. OpenBoards reicht rund 30 Tage zurück.
4. **Konfiguration nur auf dem Server:** Die Strecken stehen in der `.env`. Ein CLI-Tool auf dem Server (z. B. per SSH) bearbeitet sie interaktiv. Die Weboberfläche ist **rein lesend**.
5. **Sicher ohne Zugangsschutz:** Über die offene Oberfläche lässt sich nichts ändern, kein externer Dienst auslösen und keine Konfiguration oder Zugangsdaten auslesen.
6. **Kompatibel:** Die bestehenden Endpunkte `/api/routes`, `/api/departures` und `/api/.../stats` liefern dieselbe Struktur, das Frontend bleibt weitgehend unverändert.

## Was am alten Modell stört

| Problem | Ursache |
|---|---|
| 402.583 Zeilen, aber nur 277.442 eigenständige Abfahrten (rund 31 % Duplikate) | Jeder Zug wird **pro Strecke** gespeichert. Die S-Bahn ab Esslingen liegt so mehrfach in der Datenbank, z. B. für Plochingen und für Göppingen. |
| Zwei Polling-Schleifen (stündlich und verzögert 3–4 h) | Die alte Schnittstelle `/fchg` kennt nur aktuelle Änderungen. |
| Werte sind teils nur Prognosen und nicht endgültig | Ob ein Wert endgültig ist, ist nicht vermerkt. |
| Fehlende S-Bahn ab Stuttgart Hbf (tief), falsche Zuordnung bei Teilausfall | Die Strecke wird über Stationsnamen statt über die Bahnhofsnummer zugeordnet, und ein ausgefallener Halt wird nicht erkannt. |
| Strecken stehen in der `.env`, eine Änderung erfordert einen Neustart | Historisch gewachsen. |

## Kernidee

**Pro beobachtetem Bahnhof wird jede Abfahrt einmal gespeichert. Eine Strecke ist nur noch ein Filter darauf.**

Eine Abfahrt in Esslingen wird genau einmal gespeichert, zusammen mit ihrem Laufweg, also den folgenden Halten. Die Strecke Esslingen → Plochingen ergibt sich dann als Abfrage: alle Abfahrten in Esslingen, deren Laufweg Plochingen enthält und dort nicht ausgefallen ist. Eine neue Strecke ab einem bereits beobachteten Bahnhof braucht dadurch **keine** zusätzlichen API-Calls und hat sofort die komplette Historie.

## Datenmodell

```sql
-- Bahnhöfe: einmal angelegt, Koordinaten einmalig ermittelt
CREATE TABLE stations (
    eva        TEXT PRIMARY KEY,      -- z.B. 8000096
    stada      TEXT,                  -- Bahnhofsnummer: Hbf und Hbf (tief) teilen 6071
    name       TEXT NOT NULL,
    lat REAL, lon REAL
);

-- Spiegel der Strecken aus der .env (der Collector gleicht ab, die .env ist maßgeblich).
-- Eine aus der .env entfernte Strecke wird auf active = 0 gesetzt, ihre Daten bleiben erhalten.
CREATE TABLE routes (
    id         INTEGER PRIMARY KEY,
    origin_eva TEXT NOT NULL REFERENCES stations(eva),
    dest_eva   TEXT NOT NULL REFERENCES stations(eva),
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (origin_eva, dest_eva)
);
-- Welche Bahnhöfe abgefragt werden, ergibt sich daraus:
--   Abfahrten: SELECT DISTINCT origin_eva FROM routes WHERE active
--   Ankünfte:  SELECT DISTINCT dest_eva   FROM routes WHERE active

-- Ein Halt eines Zuges an einem beobachteten Bahnhof (Abfahrt oder Ankunft)
CREATE TABLE stop_events (
    journey_id      TEXT NOT NULL,    -- OpenBoards journeyId (stabil)
    eva             TEXT NOT NULL,    -- Bahnhof (genau: Haltestelle, z.B. Hbf tief)
    kind            TEXT NOT NULL CHECK (kind IN ('dep','arr')),
    category        TEXT,             -- S, RE, ICE …
    number          TEXT,             -- Zugnummer
    line            TEXT,             -- S1, RE5 …
    planned         TIMESTAMP NOT NULL,
    actual          TIMESTAMP,        -- best known time
    time_type       TEXT,             -- SCHEDULE | PREVIEW | REAL
    delay_min       INTEGER,
    planned_platform TEXT,
    platform        TEXT,
    cancelled       INTEGER NOT NULL DEFAULT 0,  -- dieser Halt fällt aus
    additional      INTEGER NOT NULL DEFAULT 0,  -- Zusatzhalt
    journey_cancelled INTEGER NOT NULL DEFAULT 0, -- ganze Fahrt fällt aus
    journey_type    TEXT,             -- REGULAR | REPLACEMENT | RELIEF | EXTRA
    sev_real_type   TEXT,             -- gesetzt bei Ersatzverkehr, z.B. BUS (transport.replacementTransport.realType)
    replaces_journey_id  TEXT,        -- Ersatzzug für … (replacementFor[0].journeyID)
    replaced_by_journey_id TEXT,      -- ersetzt durch … (replacedBy[0].journeyID)
    coupled_with    TEXT,             -- JSON-Liste der journeyIDs, mit denen der Zug vereinigt fährt (travelsWith)
    no_passenger_change INTEGER NOT NULL DEFAULT 0,
    cause_codes     TEXT,             -- JSON-Liste der RIS-Gründe für die Verspätung, z.B. ["43","31"]
    platform_history TEXT,            -- JSON, z.B. ["3","4","12"]
    path            TEXT,             -- nur bei 'dep': kompletter Laufweg ab hier als JSON
                                      -- [{"eva":"8001921","stada":"1717","x":0}, …]  x=1: Halt fällt aus
    final           INTEGER NOT NULL DEFAULT 0,  -- 1 = durch Tages-Batch festgeschrieben
    source          TEXT NOT NULL DEFAULT 'openboards',  -- 'iris' für Altdaten
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (journey_id, eva, kind)
);
CREATE INDEX idx_stop_events_station_time ON stop_events (eva, kind, planned);

-- Zuordnung Abfahrt → Strecke, beim Schreiben bzw. beim Anlegen einer Strecke aus `path` berechnet
-- (wie ein materialisierter Index, damit die Statistik-Abfragen kein JSON parsen müssen)
CREATE TABLE route_members (
    route_id   INTEGER NOT NULL REFERENCES routes(id),
    journey_id TEXT NOT NULL,
    status     TEXT NOT NULL,          -- cancelled|partial|sev|replaced|unknown|ok
    passenger_delay_min INTEGER,       -- bei Ersatzzug: Ist-Zeit Ersatzzug − geplante Zeit Originalzug
    PRIMARY KEY (route_id, journey_id)
);

-- Verwaltung: welche Tage endgültig abgeglichen sind
CREATE TABLE sync_state (
    eva TEXT, kind TEXT, service_date DATE,
    finalized_at TIMESTAMP,
    PRIMARY KEY (eva, kind, service_date)
);
```

**Strecke als View.** Die View hat dieselben Spalten wie die alte Tabelle `departures`, damit die bestehenden Endpunkte kaum angepasst werden müssen.

```sql
CREATE VIEW route_departures AS
SELECT m.route_id, d.journey_id, d.category, d.number,
       d.planned AS planned_dt, d.actual AS realtime_dt, d.delay_min,
       d.planned_platform, d.platform AS realtime_platform,
       m.status,                            -- cancelled|partial|sev|replaced|unknown|ok (siehe „Sonderfälle“),
                                            -- beim Schreiben von route_members berechnet
       a.delay_min AS arrival_delay_min,    -- neu: echte Ankunftsverspätung am Ziel
       d.final
FROM route_members m
JOIN routes r      ON r.id = m.route_id
JOIN stop_events d ON d.journey_id = m.journey_id AND d.eva = r.origin_eva AND d.kind = 'dep'
LEFT JOIN stop_events a ON a.journey_id = m.journey_id AND a.kind = 'arr'
     AND a.eva IN (SELECT s2.eva FROM stations s1 JOIN stations s2 ON s2.stada = s1.stada
                   WHERE s1.eva = r.dest_eva);
```

**Ablauf:**
- Beim Schreiben einer Abfahrt wird `route_members` für alle Strecken ab diesem Bahnhof gepflegt. Der Vergleich läuft über die `stada` im Laufweg.
- Beim Anlegen einer neuen Strecke wird `route_members` **einmal aus allen gespeicherten Laufwegen** berechnet. Ab einem bereits beobachteten Startbahnhof ist die Strecke damit sofort und rückwirkend vollständig, über die 30 Tage von OpenBoards hinaus.
- Größe: etwa 20 Halte × rund 60 Byte JSON pro Abfahrt. Bei rund 2.500 Abfahrten pro Tag über alle Startbahnhöfe sind das ungefähr 55 MB pro Jahr. Das ist unkritisch.

## Sonderfälle

Grundlage ist der Abgleich mit der OpenAPI-Spezifikation (Feldbeschreibungen und Enums) und mit echten Daten: 7 Tage an 4 Bahnhöfen, 20.627 Abfahrten.

| Fall | Häufigkeit in den Testdaten | Erkennung laut Spezifikation | Behandlung |
|---|---|---|---|
| **Abfahrt fällt aus** | 905 | `cancelled = true` (Haltausfall), ggf. zusätzlich `journeyInfo.journeyCancelled` | Status `cancelled`. Zählt als Ausfall, nicht in Ø Verspätung. |
| **Zurückgenommener Zusatzhalt** | selten | `cancelled` **und** `additional` gleichzeitig. Laut Spezifikation ist das kein Ausfall einer planmäßigen Fahrt. | Wird ignoriert, keine Statistik |
| **Teilausfall: Ziel wird nicht erreicht** | 1.403 Fahrten mit ausgefallenen Halten | Ziel-Halt in `via[]` hat `cancelled = true`. Oder die Fahrt endet vorher (`journeyInfo.destination.cancelled` und `differingDestination` liegt vor dem Ziel). Oder in der Ankunftstafel am Ziel ist `cancelled = true`. | Status `partial`. Zählt für die Strecke als Ausfall, wird aber getrennt ausgewiesen. |
| **Fahrt beginnt später** | 599 (`differingOrigin`) | Der Halt an unserem Startbahnhof hat `cancelled = true` | wie „Abfahrt fällt aus“ |
| **Ersatzzug** | 59 bzw. 57 | `journeyInfo.type = REPLACEMENT`, `replacementFor[]` am Ersatzzug, `replacedBy[]` am ausgefallenen Zug | Der ausgefallene Zug bekommt den Status `cancelled` und einen Verweis auf den Ersatzzug. Der Ersatzzug wird **nicht** als eigene zusätzliche Fahrt gezählt. Seine Ist-Zeit wird an der Fahrt des Originalzugs ausgewertet: **Verspätung für Reisende = Ist-Zeit des Ersatzzugs − geplante Zeit des Originalzugs**. Status `replaced`. |
| **Schienenersatzverkehr (SEV)** | 30 Busse an 2 Bahnhöfen in 3 Tagen | `transport.replacementTransport` ist gesetzt, `realType = BUS`. ⚠️ `transport.type` steht dabei auf `REGIONAL_TRAIN`, die Kategorie ist `Bus`. | Status `sev`. Wird nicht in die Pünktlichkeit eingerechnet und separat als „Ersatzverkehr“ angezeigt. Hat nie eine echte Ist-Zeit. Ersetzt der Bus einen Zug der Strecke, wird dieser Zug als ausgefallen mit Ersatz gewertet. |
| **Entlastungszug, Sonderfahrt** | 224 `EXTRA` | `journeyInfo.type = RELIEF` oder `EXTRA`, `reliefFor[]` | Wird normal gezählt, aber markiert. In der Statistik kann man umschalten, ob nur der reguläre Fahrplan zählt. |
| **Zusatzhalt** | 47 | `additional = true` | wie Entlastungszug: zählt, ist aber markiert |
| **Vereinigte Züge (Flügelzüge)** | 1.175 | `travelsWith[]` mit `separationAt` | Mehrere Zugnummern, aber ein physischer Zug. Alle Teile werden gespeichert. In der Statistik zählt der physische Zug einmal, gruppiert über die geplante Zeit und `coupled_with`. Der Teil, der das Ziel nicht bedient, gehört ohnehin nicht zur Strecke. |
| **Keine Ist-Zeit trotz Vergangenheit** | 60 in 3 Tagen | `timeType` ist `PREVIEW` oder `SCHEDULE`, obwohl der Abschluss-Lauf die Fahrt nach Ende der Fahrt abfragt, und die Fahrt ist nicht ausgefallen | Status `unknown`, der Wert gilt nicht als Ist-Wert. **Nicht** als pünktlich zählen, separat ausweisen. Der alte Code setzt hier 0 Minuten, das verfälscht die Statistik. |
| **Kein Ein-/Ausstieg** | selten | `noPassengerChange = true` am Start oder Ziel | Für Reisende wie ein Ausfall: `cancelled` bzw. `partial` |
| **Gleiswechsel** | 1.626 | `platform ≠ platformSchedule`, Verlauf in `platformHistory` | Speichern und anzeigen. Keine Auswirkung auf die Pünktlichkeit. |
| **Andere Zugnummer** | häufig bei Ersatz und Durchbindung | `headerJourneyNumber` ≠ `journeyNumber`. `continuationFor[]` bedeutet Durchbindung. | Schlüssel ist immer `journeyId`, nie die Zugnummer. Angezeigt wird `journeyNumber`. |
| **Verspätungsgrund** | in fast allen Einträgen gibt es `messages` | `messages.risCauseCodes[].code`, Codes 00–99. Laut Spezifikation ist die Liste „not exhaustive“, es können also unbekannte Codes kommen. | Wird gespeichert. **Neu in der UI:** häufigste Gründe pro Strecke, z. B. „Bauarbeiten“ oder „Verspätung eines vorausfahrenden Zuges“. Unbekannte Codes erscheinen als „Sonstiger Grund (Code xx)“. |
| **Verspätung über Mitternacht** | – | Das Zeitfenster filtert nach der Ist-Zeit | Der Abschluss-Lauf fragt mit 4 h Puffer ab, gespeichert wird nach der Fahrplanzeit |

**Statuswerte einer Fahrt auf einer Strecke**, nach Priorität:
`cancelled` > `partial` > `sev` > `replaced` > `unknown` > `ok`.
Zusätzlich gibt es die Merker `extra`, `additional` und `platform_changed`.

**Kennzahlen:**
- **Pünktlich** heißt Verspätung unter 6 Minuten. So definiert die DB ihre Pünktlichkeit offiziell.
- Basis sind alle Fahrten mit Status `ok` oder `replaced` und echter Ist-Zeit.
- **Ausfallquote** = (`cancelled` + `partial`) / alle planmäßigen Fahrten.
- Zusätzlich gibt es eine **Pünktlichkeit aus Reisendensicht**: Hier gelten auch Ausfälle und Teilausfälle als unpünktlich.

**Altdaten:**
- 13.570 Zeilen mit Status `c` werden als `cancelled` übernommen.
- 1.322 Zeilen mit Status `p` werden als `partial` übernommen.
- 71 nicht ausgefallene Zeilen ohne Ist-Zeit werden als `unknown` übernommen, nicht mehr als pünktlich.
- Ersatzzüge und SEV wurden im alten System nicht erkannt. Für die letzten 29 Tage werden sie durch den Neuabschluss korrigiert, ältere Daten bleiben ungenau.

## Abfragelogik

Es gibt nur noch **einen** Collector mit zwei Modi. Beide verwenden dieselbe Funktion `sync_board(eva, kind, start, end, final)`.

**Live, alle 10 min:**
- Fenster: jetzt − 60 min bis jetzt + 90 min
- Abgefragt werden Abfahrten an allen Startbahnhöfen und Ankünfte an allen Zielbahnhöfen.
- Upsert über `(journey_id, eva, kind)`. Ein Eintrag mit `final = 1` wird **nie** von Live-Daten überschrieben.

**Abschluss täglich, z. B. 03:00:**
- Für jeden Bahnhof und jeden Tag ohne Eintrag in `sync_state` (höchstens 29 Tage zurück) wird der Tag 00:00–24:00 abgefragt, plus 4 h Puffer für Verspätungen über Mitternacht.
- Alle Halte des Tags werden mit `final = 1` geschrieben. Halte, die live gesehen wurden, aber im Batch fehlen, werden als „entfallen“ markiert.
- Dadurch holt derselbe Mechanismus automatisch Ausfallzeiten des Servers nach.

**Sehr späte Züge (Nachzügler):**
- Gespeichert wird alles, was die Tafel liefert und dessen Planzeit vor dem Ende des Fensters liegt. Eine Untergrenze für die Planzeit gibt es nicht.
- Ein Zug mit Plan 19:00 und 10 h Verspätung erscheint dadurch live, sobald seine Prognose ins Fenster rückt.
- Endgültig wird er beim Abschluss des **Folgetags** gespeichert, ohne zusätzliche Requests.
- Der Abschluss des Plantags löscht nur Live-Zeilen, deren (prognostizierte) Zeit innerhalb des abgefragten Fensters liegt.
- Damit sind Verspätungen bis etwa 24 h abgedeckt.

**Nachladen bei neuer Strecke:** Neuer Start- oder Zielbahnhof wird angelegt → für diesen Bahnhof die letzten 29 Tage über den Abschluss-Lauf füllen.

**API-Details, die alle in `sync_board` stecken:**
- `maxViaStops = 100`, sonst fehlen Halte.
- Ein Fenster wird geteilt, wenn `meta.truncatedTransportTypes` gesetzt ist.
- Das Zeitfenster gilt für die tatsächliche Zeit. Deshalb hat der Abschluss-Lauf den 4-h-Puffer, gespeichert wird aber nach Fahrplanzeit.
- Pro Lauf und Bahnhof wird nur einmal abgefragt, auch wenn mehrere Strecken denselben Startbahnhof haben.
- Höchstens 10 Anfragen pro Sekunde, Retry bei 429, 5xx und Verbindungsfehlern.

**Request-Bedarf** (deine 20 Strecken: 10 Start- und 10 Zielbahnhöfe, viele davon identisch):

| | ca. Requests |
|---|---|
| Live: 10 Bahnhöfe × 2 Arten × 6 pro Stunde | rund 120 pro Stunde, etwas mehr bei vollen Fenstern am Hbf |
| Abschluss: pro Tag | rund 250 |

Das liegt weit unter dem Limit des Free-Plans.

## Konfiguration: `.env` und CLI-Tool

**Die `.env` ist die einzige Quelle der Konfiguration.** Die Weboberfläche kann sie weder lesen noch ändern.

```env
DB_CLIENT_ID=…
DB_API_KEY=…

# Strecken als EVA-Nummern: "<->" = beide Richtungen, "->" = eine Richtung, getrennt mit ";"
# Die Namen in Klammern sind optional und dienen nur der Lesbarkeit. Das CLI-Tool schreibt sie automatisch.
ROUTES=8001920(Esslingen)<->8000096(Stuttgart Hbf);8001920(Esslingen)<->8000302(Plochingen)

LIVE_INTERVAL=600        # Sekunden, Standard 600
FINALIZE_AT=03:00        # tägliche Abschlusszeit
WEB_HOST=0.0.0.0
WEB_PORT=8080
```

- **Altes Format bleibt nutzbar:** `POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf` mit Namen wird weiterhin gelesen. Die Namen müssen bereits in der lokalen Tabelle `stations` bekannt sein. Ist ein Name unbekannt oder nicht eindeutig, steht ein Fehler im Log, und die EVA-Nummer sollte eingetragen werden.
- **Kein Neustart nötig:** Der Collector liest die `.env` vor jedem Durchlauf neu ein, sobald sich das Änderungsdatum der Datei geändert hat. Dann gleicht er die Tabelle `routes` ab.
  - Neue Strecke: `route_members` wird aus dem Bestand berechnet. Für neue Bahnhöfe werden die letzten 29 Tage nachgeladen.
  - Entfernte Strecke: wird auf `active = 0` gesetzt und erscheint nicht mehr im Web. Die Daten bleiben erhalten, ein erneutes Hinzufügen macht sie wieder sichtbar.
  - In Docker wird die `.env` dafür als Datei in den Container gemountet, statt sie nur als `env_file` beim Start zu übergeben.
- Die Variablen `POLLING_ENABLED`, `POLLING_INTERVAL` und `DELAYED_POLLING_*` entfallen.

### CLI-Tool `dbmon` (auf dem Server, z. B. per SSH)

```
dbmon routes                  Strecken auflisten (Name, EVA, aktiv, Datenstand, letzte Abfrage)
dbmon add                     interaktiv: Start suchen → Treffer wählen → Ziel suchen → Richtung wählen
dbmon add 8001920 8000096 --both   direkt per EVA-Nummer, ohne Suche
dbmon remove                  interaktiv aus der Liste wählen
dbmon search "Esslingen"      Suche in den bereits gesammelten Halten, zeigt Name und EVA
dbmon status                  Collector-Status: letzte Live-Abfrage, abgeschlossene Tage, Lücken, Fehler
dbmon backfill [--days 29]    fehlende Tage sofort nachholen, statt bis 03:00 zu warten
```

- **Bahnhofssuche:** Die Suche läuft lokal über die Halte, die bisher gesammelt wurden. Ist ein Bahnhof unbekannt, wird die EVA-Nummer direkt eingegeben und einmal mit einem OpenBoards-Call geprüft. Der Call liefert auch Name und `stada`.
- **Schreiben der `.env`:** Das Tool ändert nur die Zeile `ROUTES=` und behält alle anderen Zeilen und Kommentare bei. Vorher legt es eine Sicherung `.env.bak` an. Geschrieben wird atomar, also erst in eine temporäre Datei, dann umbenannt.
- **Docker:** `docker compose exec collector dbmon add`. Docker legt damit keinen eigenen Zugang an, `dbmon` ist ausschließlich für Benutzer mit Shell-Zugang zum Server erreichbar.

## Sicherheit der öffentlichen Weboberfläche

**Grundprinzip:** Das Web liest nur und hat keine Schlüssel. Datensammlung und Konfiguration laufen in einem getrennten Prozess.

| Maßnahme | Heute | Neu |
|---|---|---|
| Getrennte Prozesse | Polling läuft im Webserver | Zwei Prozesse bzw. Container: `collector` mit API-Keys und Schreibzugriff, `web` ohne beides |
| Schreibschutz der DB | Webprozess öffnet die DB schreibend | Webprozess öffnet SQLite mit `mode=ro`. Selbst ein Fehler im Code kann dann nichts schreiben. |
| Keine Zugangsdaten im Web | `.env` mit Keys wird auch in den Webprozess geladen | Der `web`-Container bekommt **keine** `DB_*`-Variablen und keine `.env` |
| Keine externen Calls auf Anfrage | `GET /api/routes` löst Nominatim-Geocoding aus und schreibt das Ergebnis in die DB | Koordinaten ermittelt nur der Collector, das Web liefert gespeicherte Werte |
| Nur GET-Endpunkte | ja | ja, und kein Endpunkt mit Schreibzugriff, keine Stationssuche |
| Keine Konfiguration nach außen | `/api/polling/status` zeigt Strecken und Intervalle | Entfällt. `/api/health` liefert nur „ok“ und den Zeitpunkt der letzten Datenaktualisierung. |
| API-Doku | `/docs`, `/redoc`, `/openapi.json` öffentlich | abgeschaltet (`docs_url=None` usw.) |
| CORS | `allow_origins=["*"]` | entfernt. Die Oberfläche läuft auf demselben Origin und braucht kein CORS. |
| XSS | Daten werden per `innerHTML` in die Seite eingesetzt, Werte aus der Bahn-API unescaped | Alle Werte aus der DB laufen durch eine `escapeHtml()`-Funktion oder werden per `textContent` gesetzt. Das ist wichtig, weil OpenBoards neue Freitexte wie Linie oder Hinweise liefert. |
| Security-Header | keine | `Content-Security-Policy` (nur eigene Skripte plus die genutzten CDNs), `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY` |
| Teure Abfragen begrenzen | `limit` bis 5000, Suchbegriff `q` beliebig lang, Zeitraum bis 1 Jahr | `q` höchstens 50 Zeichen, Zeiträume begrenzt, Statistiken mit kurzem Cache (60 s), damit wiederholte Aufrufe die DB nicht belasten |
| Fehlermeldungen | Ausnahmetexte gehen teils an den Client | Generische Fehlermeldung nach außen, Details nur ins Log |
| Netz | Port 8080 auf allen Interfaces | unverändert konfigurierbar. Empfohlen: Reverse Proxy (Caddy oder nginx) mit TLS und Rate-Limit davor. Ein Beispiel dafür kommt in `DOCKER.md`. |
| Container | läuft als root | läuft als eigener Nutzer ohne Rechte, Dateisystem schreibgeschützt bis auf das Datenvolume |

## Migration der Altdaten

1. Bestehende Zeilen aus `departures` werden nach `stop_events` übernommen, mit `source = 'iris'`, `final = 1` und `journey_id = 'iris:' || service_id`. Duplikate über mehrere Strecken fallen dabei weg.
2. Für Altdaten gibt es keinen Laufweg. Die Zuordnung zur Strecke wird daher direkt aus der alten `route_id` nach `route_members` übernommen.
3. Die letzten 29 Tage werden mit OpenBoards neu abgeschlossen. Wo derselbe Zug in beiden Quellen vorkommt (gleiche Zugnummer und Fahrplanzeit), ersetzt der OpenBoards-Eintrag den Alteintrag. Damit werden auch die fehlenden S-Bahnen ab Hbf (tief) nachgetragen.
4. Die alte Tabelle wird in `departures_legacy` umbenannt und erst nach einer Prüfung gelöscht.

## Weboberfläche: Neugestaltung

### Probleme heute
- Keine einzige Media Query: Auf dem Handy sind Tabelle, Filter und Modals schwer lesbar und bedienbar.
- Streckeninfos auf der Karte gibt es nur per **Hover**, das funktioniert auf Touch-Geräten nicht.
- Plotly (rund 3,5 MB) und Leaflet werden von fremden CDNs geladen. Das ist langsam auf dem Handy und ein Datenschutzthema, weil die Seite eine Datenschutzerklärung hat.
- Statistiken werden teils aus bis zu 5.000 Rohzeilen im Browser berechnet.
- Deutsche und englische Beschriftungen sind gemischt. Der Verspätungsstatus ist nur farblich kodiert.

### Seitenstruktur

| Seite | Inhalt |
|---|---|
| **Übersicht** `/` | **Streckenkarten** für jede Strecke, Hin- und Rückrichtung zusammengefasst (A ⇄ B). Jede Karte zeigt: Pünktlichkeit der letzten 7 Tage (≤ 5 min), Ø Verspätung, Ausfälle, eine Mini-Verlaufskurve über 30 Tage und die nächsten 3 Abfahrten mit Live-Verspätung. Dazu eine **Karte**: Bahnhöfe als Punkte, Strecken als Linien in der Farbe ihrer Pünktlichkeit. Ein Tipp oder Klick öffnet die Strecke. |
| **Strecke** `/route/{id}` | Ein Umschalter für die Richtung. Zeitraum über Chips: 7 Tage, 30 Tage, 90 Tage, 1 Jahr, eigener Zeitraum. Inhalte: (1) **Kennzahlen**: Pünktlichkeit bei Abfahrt **und Ankunft**, Ø und Median der Verspätung, Ausfälle, Teilausfälle, Anzahl Fahrten. (2) **Live**: zuletzt abgefahrene und nächste Züge mit Gleis und Status. (3) **Heatmap** Wochentag × Uhrzeit: „Wann ist es am schlimmsten?“. (4) **Verlauf**: Pünktlichkeit pro Tag bzw. Woche. (5) **Verteilung**: pünktlich / 1–5 / 6–15 / > 15 min / Ausfall. (6) **Problemzüge**: Zugnummern bzw. Linien mit der schlechtesten Pünktlichkeit. (7) **Fahrtenliste** mit Filtern (Zug, Status, nur verspätet). |
| **Bahnhof** `/station/{eva}` | Alle Strecken ab diesem Bahnhof, eine Live-Abfahrtstafel der beobachteten Züge und Kennzahlen für den Bahnhof. |
| **Vergleich** `/stats` | Ersetzt die heutige `details.html`: alle Strecken nebeneinander, sortierbar nach Pünktlichkeit, Ø Verspätung und Ausfallquote, über denselben Zeitraumfilter. |

### Mobil und Desktop
- **Mobile first:** Das Layout ist zuerst einspaltig und bekommt ab 768 px und 1100 px mehr Spalten. Auf dem Desktop stehen auf der Übersicht Karte und Streckenliste nebeneinander, auf dem Handy wechselt man per Umschalter zwischen Liste und Karte.
- **Fahrtenliste:** auf dem Handy als kompakte Karten, eine Fahrt pro Karte. Auf dem Desktop als Tabelle mit fester Kopfzeile. Weitere Einträge über „Mehr laden“.
- **Bedienung:**
  - Alles per Tipp oder Klick statt Hover, Tooltips öffnen per Antippen. Klickflächen sind mindestens 44 px groß.
  - Unten gibt es eine Navigationsleiste auf dem Handy, oben auf dem Desktop.
  - Filter öffnen sich auf dem Handy als Panel von unten.
- **Diagramme:** Chart.js 4 statt Plotly (rund 200 KB statt 3,5 MB), mit Touch-Tooltips. Die Heatmap ist als CSS-Grid gebaut, so bleibt sie auch auf schmalen Displays lesbar.
- **Lesbarkeit:**
  - Status wird immer mit Farbe **und** Text oder Symbol dargestellt, z. B. „+12“ mit Badge.
  - Ausreichender Kontrast, Dunkelmodus über `prefers-color-scheme`.
  - Alle Beschriftungen einheitlich auf Deutsch, Zeiten im Format `HH:MM`.
- **Teilbare URLs:** Strecke, Zeitraum und Filter stehen in der URL. Damit funktionieren der Zurück-Button und Lesezeichen.

### Technik
- **Keine Build-Pipeline:** weiterhin reines HTML, CSS und JavaScript, aber aufgeteilt in Module (`api.js`, `charts.js`, `pages/*.js`) und ein gemeinsames Stylesheet mit CSS-Variablen.
- **Bibliotheken selbst ausliefern** (Leaflet und Chart.js unter `/static/vendor/`): kein CDN, und die Content-Security-Policy kann auf `'self'` beschränkt werden. Nur die Kartenkacheln kommen weiter von OpenStreetMap, das wird in der Datenschutzerklärung erwähnt.
- **Neue, rein lesende Endpunkte**, die bereits aggregierte Daten liefern. Alle sind GET-Anfragen und werden 60 s zwischengespeichert:

  ```
  GET /api/overview                         Streckenkarten inkl. Kennzahlen und nächster Abfahrten
  GET /api/routes/{id}/summary?period=30d   Kennzahlen (Abfahrt + Ankunft)
  GET /api/routes/{id}/heatmap?period=90d   Wochentag × Stunde
  GET /api/routes/{id}/trend?period=1y      Pünktlichkeit pro Tag/Woche
  GET /api/routes/{id}/trains?period=30d    Problemzüge
  GET /api/routes/{id}/live                 letzte und nächste Fahrten
  GET /api/stations/{eva}                   Bahnhofsseite
  GET /api/departures …                     Fahrtenliste (wie bisher, mit Seitenweise-Laden)
  ```

- **Prüfung vor der Fertigmeldung:** jede Seite im Browser bei 375 px (Handy), 768 px (Tablet) und 1440 px (Desktop), jeweils im hellen und dunklen Modus.

## Bahnhofsmonitor (Ergänzung)

- **Beobachtete Bahnhöfe:** alle Start- und Zielbahnhöfe der Strecken, dazu `STATIONS` aus der `.env`. Jeder dieser Bahnhöfe wird für Abfahrten **und** Ankünfte abgefragt.
- **Spalten `st_status`, `st_delay` und `st_real` in `stop_events`:** die Sicht des Bahnhofs auf jeden Zug. Die Regeln sind dieselben wie bei den Strecken, aber ohne Ziel. Mögliche Werte: `ok`, `cancelled`, `replaced`, `replacement`, `sev`, `unknown`, `ignore`. Berechnet wird das in `classify_station()` nach jedem Abruf einer Tafel.
- **Herkunft bei Ankünften** über die neue Spalte `origin_eva` (aus `journeyInfo.origin`). Damit gibt es die Aufschlüsselung „nach Linie und Richtung“ auch für Ankünfte.
- **Kartenverlauf:** `route_geometry()` nimmt aus den gespeicherten Laufwegen ein häufiges Muster, und zwar das mit den meisten Halten (mindestens 30 % so häufig wie das häufigste). Die Koordinaten der Zwischenhalte kommen von Nominatim. Ein Ergebnis wird verworfen, wenn es außerhalb des Rechtecks um Start und Ziel liegt, dann wird der Halt übersprungen.
- **Bahnhofsstatistiken** nutzen nur OpenBoards-Daten, weil die Altdaten nur die Züge der Strecken enthalten.

## Speicher-Schema 3 (umgesetzt)

Gemessen auf der echten Datenbank mit 475.086 Halten und 413.518 Streckenfahrten:

| | vorher (Schema 2) | nachher (Schema 3) |
|---|---|---|
| eigentliche Daten inkl. Indizes | 332 MB | 130 MB (−61 %) |
| Datei gesamt (inkl. unveränderter `departures_legacy`) | 420 MB | 212 MB |
| Übersicht, 54 Strecken (ohne Cache) | 243 ms | 124 ms |
| Vergleich Strecken, 12 Monate | 243 ms | 87 ms |

- **Fahrt-IDs:** Die lange OpenBoards-ID steht nur noch einmal in `journeys`. Überall sonst steht eine Ganzzahl (`jid`), auch bei Ersatz- und Flügelzug-Verweisen.
- **Zeiten:** Unix-Sekunden. Die lokale Zeit (Europe/Berlin) wird beim Ausliefern exakt zurückgerechnet.
- **Laufweg:** `8001921,8006643x,8000096`. `x` steht für einen ausgefallenen, `a` für einen zusätzlichen Halt. Die `stada` kommt aus `stations` und wird nur bei Abweichung als `:stada` mitgeschrieben.
- **`stop_events` und `route_trips`** sind als `WITHOUT ROWID` nach (Bahnhof, Art, Zeit) bzw. (Strecke, Zeit) sortiert. Zeitraumabfragen lesen dadurch zusammenhängende Seiten.
- **Tageszusammenfassungen** (`agg_hour`, `agg_line`, `agg_cause`, `agg_done`) werden beim Abschluss eines Tages geschrieben. Die Web-API nimmt für abgeschlossene Tage die Zusammenfassungen und rechnet den Rest mit derselben Funktion `aggregate()` aus den Rohdaten. Kennzahlen summiert SQLite direkt.
- **Migration:** Die neuen Tabellen entstehen neben den alten. Jede Zeile wird zurückgewandelt und Feld für Feld verglichen, erst dann werden die Tabellen in einer Transaktion getauscht.
- **Abgleich der Kennzahlen alt gegen neu:** 2.031 Auswertungen, keine echte Abweichung. 32 Fälle unterscheiden sich nur bei Gleichstand an einer Top-N-Grenze. 39 Fälle betreffen einen Durchschnitt, der exakt auf ,x5 endet: Die alte Version rundete ihn wegen Binär-Gleitkomma um 0,1 zu niedrig.
- **`departures_legacy`** (Original der Altdaten) bleibt bewusst erhalten.

## Code-Struktur

```
dbmonitor/openboards.py  Client: board(), board_range() (Fenster teilen, Retry, Rate-Limit)
dbmonitor/stations.py    lokale Bahnhofssuche, Koordinaten über Nominatim (nur Collector)
dbmonitor/config.py      .env lesen und schreiben, ROUTES parsen (neues und altes Format)
dbmonitor/storage.py     Schema, Migration, Upserts, Klassifizierung route_trips (Sonderfälle)
dbmonitor/collector.py   eigener Prozess: Live-Takt, täglicher Abschluss, Nachladen, Geocoding, Abgleich mit der .env
dbmonitor/cli.py         CLI dbmon (Aufruf über das Skript ./dbmon)
dbmonitor/web.py         FastAPI rein lesend: DB mit mode=ro, keine Keys, Security-Header, aggregierte Endpunkte
static/                  index.html, route.html, station.html, stats.html, privacy.html, css/app.css,
                         js/ui.js, js/pages/*.js, vendor/{leaflet,chart.umd}.js
docker-compose.yml       zwei Dienste (collector, web) mit gemeinsamem Datenvolume, web ohne .env
```

**Abweichungen vom Entwurf bei der Umsetzung:**
- `route_members` und die View wurden zu einer Tabelle **`route_trips`** zusammengefasst. Sie enthält alle Werte, die die Oberfläche braucht. Dadurch müssen die Statistik-Abfragen nichts mehr zusammenführen und laufen schnell.
- `stop_events` hat als Schlüssel `(journey_id, board_eva, kind)`. Die tatsächliche Haltestelle steht in `stop_eva`, denn die Tafel von Stuttgart Hbf enthält auch die Haltestellen im Tiefbahnhof und die SEV-Bushaltestellen.
- **Bahnhofssuche:** Es wird **keine weitere API** genutzt. RIS::Stations ist kostenpflichtig (ab 4.200 €/Jahr), und auch StaDa wurde bewusst nicht eingebunden. Gesucht wird lokal in der Tabelle `stations`. Sie wird aus allen OpenBoards-Antworten gefüllt, die die Halte aller Züge enthalten (im Test schon nach 3 Tagen 883 Halte). Unbekannte Bahnhöfe werden per EVA-Nummer eingetragen und über OpenBoards geprüft.
- **Kartenkacheln:** Die CARTO-Kacheln verlangen inzwischen einen API-Key. Verwendet werden deshalb die Standardkacheln von OpenStreetMap, im Dunkelmodus per CSS-Filter abgedunkelt.

## Entscheidungen (20.11.2026)

1. **Echte Ankunftsverspätung:** ja. Ankunftstafeln an allen Zielbahnhöfen werden mit abgefragt.
2. **Bahnhofssuche:** nur im CLI-Tool, lokal aus den gesammelten Daten oder per EVA-Nummer. Keine zusätzliche API. Die Weboberfläche bekommt keine Suche.
3. **Speicherumfang:** alle Abfahrten an beobachteten Startbahnhöfen, inklusive Laufweg.
4. **Konfiguration:** nur über die `.env` und das CLI-Tool `dbmon`. Die Weboberfläche ist rein lesend: Abfrage, Karte, Statistiken.
5. **Oberfläche:** Übersicht, Strecken- und Statistikseite werden neu gestaltet, mit gleicher Lesbarkeit und Bedienbarkeit auf Handy und Desktop.

## Noch zu klären bei der Umsetzung

- Ob sich `REAL`-Werte nach dem Abschluss noch ändern. Zur Prüfung kann man einen Tag nach einer Woche erneut abschließen und die Werte vergleichen.
