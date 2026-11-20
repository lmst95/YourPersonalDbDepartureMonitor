"""dbmon – configure routes in .env and inspect the collector, on the server (e.g. via SSH).

  dbmon routes                 configured routes with data status
  dbmon add [ORIGIN DEST]      add a route (interactive search, or EVA numbers); --one-way
  dbmon remove                 remove a route (interactive)
  dbmon stations               stations observed on their own (STATIONS)
  dbmon add-station [EVA]      observe a station without a route (e.g. an intermediate stop)
  dbmon remove-station         stop observing such a station
  dbmon search TEXT            search stations known from collected data (name -> EVA number)
  dbmon status                 collector status, finalized days, gaps, last error
  dbmon backfill [--days N]    finalize missing days now (needs API keys)
  dbmon import-db              write the routes stored in the database into ROUTES (.env)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import TZ, storage
from .config import load_settings, parse_routes, write_routes, write_stations
from .openboards import OpenBoardsClient, OpenBoardsError
from .stations import search_local


def _pairs_from_settings(settings) -> List[Tuple[str, str, str, str]]:
    pairs = []
    for r in settings.routes:
        if not (r.origin.eva and r.dest.eva):
            continue
        pairs.append((r.origin.eva, r.origin.label or "", r.dest.eva, r.dest.label or ""))
    return pairs


def _db(settings, readonly=True):
    path = Path(settings.db_path)
    if not path.exists():
        return None
    conn = storage.connect(str(path), readonly=readonly)
    return conn


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


# ------------------------------------------------------------------ station picking
EVA_HELP = (
    "  Die EVA-Nummer (auch IBNR) ist die 7-stellige Haltestellennummer der Bahn, z.B. 8000096 = Stuttgart Hbf.\n"
    "  So findest du sie:\n"
    "   * dbmon search NAME – sucht in allen Halten, die der Collector schon gesehen hat (alle Halte der Züge\n"
    "     an deinen Bahnhöfen, also meist das ganze Umland)\n"
    "   * bahn.de: Verbindung suchen, den Link teilen/kopieren – darin steht der Bahnhof als '@L=8000096@'\n"
    "  Die Nummer wird beim Hinzufügen über OpenBoards geprüft und der Name automatisch ermittelt."
)


def _resolve_eva(settings, eva: str) -> Optional[Tuple[str, str]]:
    """(eva, name) for a directly entered EVA number: known locally, else verified via OpenBoards."""
    conn = _db(settings)
    if conn:
        row = conn.execute("SELECT name, stada FROM stations WHERE eva = ?", (eva,)).fetchone()
        if row and row["stada"]:
            return eva, row["name"]
    try:
        sp = OpenBoardsClient(settings.client_id, settings.api_key).lookup_stop_place(eva)
    except (OpenBoardsError, ValueError) as e:
        print(f"  Prüfung über OpenBoards fehlgeschlagen: {e}")
        return None
    if not sp:
        print(f"  EVA {eva} liefert keine Abfahrten/Ankünfte – Nummer prüfen.")
        return None
    return eva, sp["name"]


def _pick_station(settings, label: str) -> Tuple[str, str]:
    conn = _db(settings)
    while True:
        q = _ask(f"{label} – Bahnhofsname oder EVA-Nummer (? = Hilfe): ")
        if not q:
            continue
        if q == "?":
            print(EVA_HELP)
            continue
        if q.isdigit():
            res = _resolve_eva(settings, q)
            if res:
                print(f"  → {res[1]} ({res[0]})")
                return res
            continue
        hits = search_local(conn, q) if conn else []
        if not hits:
            print("  Nicht in den bisher gesammelten Daten gefunden – bitte EVA-Nummer eingeben (? = Hilfe).")
            continue
        for i, h in enumerate(hits[:15], 1):
            print(f"  {i:2}) {h['name']:<40} EVA {h['eva']}{'' if h['stada'] else '  (Bus/Tram)'}")
        sel = _ask("  Nummer wählen (Enter = neu suchen): ")
        if sel.isdigit() and 1 <= int(sel) <= min(15, len(hits)):
            h = hits[int(sel) - 1]
            return h["eva"], h["name"]


# ------------------------------------------------------------------ commands
def cmd_routes(settings, _args) -> int:
    for e in settings.route_errors:
        print(f"! {e}")
    if not settings.routes_configured:
        print("Keine ROUTES in der .env. Mit 'dbmon import-db' können die Strecken aus der Datenbank übernommen werden.")
    conn = _db(settings)
    stats = {}
    if conn:
        for r in conn.execute("""
            SELECT r.origin_eva, r.dest_eva, r.active, COUNT(t.jid) n, MIN(t.planned) first, MAX(t.planned) last
            FROM routes r LEFT JOIN route_trips t ON t.route_id = r.id GROUP BY r.id"""):
            stats[(r["origin_eva"], r["dest_eva"])] = r
    names = {}
    if conn:
        names = {r["eva"]: r["name"] for r in conn.execute("SELECT eva, name FROM stations")}
    print(f"{'Start':<28} {'Ziel':<28} {'Fahrten':>8}  Zeitraum")
    for r in settings.routes:
        o = r.origin.eva or r.origin.name
        d = r.dest.eva or r.dest.name
        st = stats.get((o, d))
        on = names.get(o) or r.origin.display()
        dn = names.get(d) or r.dest.display()
        span = (f"{storage.iso_local(st['first'])[:10]} – {storage.iso_local(st['last'])[:10]}"
                if st and st["first"] is not None else "noch keine Daten")
        print(f"{on[:27]:<28} {dn[:27]:<28} {(st['n'] if st else 0):>8}  {span}")
    inactive = [k for k, v in stats.items() if not v["active"]]
    if inactive:
        print(f"\n{len(inactive)} entfernte Strecke(n) mit gespeicherten Daten (nicht im Web sichtbar).")
    return 0


def cmd_add(settings, args) -> int:
    if args.origin and args.dest:
        o = _resolve_eva(settings, args.origin)
        d = _resolve_eva(settings, args.dest)
        if not (o and d):
            return 1
        both = not args.one_way
    else:
        o = _pick_station(settings, "Start")
        d = _pick_station(settings, "Ziel")
        both = _ask("Beide Richtungen beobachten? [J/n] ").lower() not in ("n", "nein")
    if o[0] == d[0]:
        print("Start und Ziel sind gleich.")
        return 1
    pairs = _pairs_from_settings(settings)
    new = [(o[0], o[1], d[0], d[1])] + ([(d[0], d[1], o[0], o[1])] if both else [])
    for p in new:
        if not any(p[0] == q[0] and p[2] == q[2] for q in pairs):
            pairs.append(p)
    value = write_routes(settings.env_path, pairs)
    print(f"Gespeichert in {settings.env_path}:\nROUTES={value}")
    print("Der Collector übernimmt die Änderung beim nächsten Durchlauf (≤ 30 s) und lädt die letzten Tage nach.")
    return 0


def cmd_remove(settings, _args) -> int:
    pairs = _pairs_from_settings(settings)
    if not pairs:
        print("Keine Strecken mit EVA-Nummern konfiguriert.")
        return 1
    for i, (o, on, d, dn) in enumerate(pairs, 1):
        print(f"  {i:2}) {on or o} → {dn or d}")
    sel = _ask("Welche Strecke entfernen? (Nummer, mehrere mit Komma): ")
    idx = {int(x) for x in sel.replace(" ", "").split(",") if x.isdigit()}
    keep = [p for i, p in enumerate(pairs, 1) if i not in idx]
    if len(keep) == len(pairs):
        print("Nichts geändert.")
        return 0
    value = write_routes(settings.env_path, keep)
    print(f"ROUTES={value}\nDie gespeicherten Daten bleiben erhalten; die Strecke ist im Web nicht mehr sichtbar.")
    return 0


def _station_items(settings) -> List[Tuple[str, str]]:
    return [(r.eva, r.label or "") for r in settings.stations if r.eva]


def cmd_stations(settings, _args) -> int:
    items = _station_items(settings)
    route_evas = {x for r in settings.routes for x in (r.origin.eva, r.dest.eva) if x}
    print(f"Start-/Zielbahnhöfe der Strecken: {len(route_evas)} (werden automatisch beobachtet)")
    if not items:
        print("Keine zusätzlichen Bahnhöfe (STATIONS). Hinzufügen mit: dbmon add-station")
        return 0
    conn = _db(settings)
    for eva, label in items:
        n = conn.execute("SELECT COUNT(*) FROM stop_events WHERE board_eva = ? AND iris = 0",
                         (eva,)).fetchone()[0] if conn else 0
        print(f"  {label or eva:<36} EVA {eva:<9} {n:>7} Halte gespeichert")
    return 0


def cmd_add_station(settings, args) -> int:
    if args.eva:
        res = _resolve_eva(settings, args.eva)
        if not res:
            return 1
    else:
        res = _pick_station(settings, "Bahnhof")
    items = _station_items(settings)
    if any(e == res[0] for e, _ in items):
        print(f"{res[1]} wird bereits beobachtet.")
        return 0
    items.append(res)
    value = write_stations(settings.env_path, items)
    print(f"Gespeichert in {settings.env_path}:\nSTATIONS={value}")
    print("Der Collector übernimmt die Änderung beim nächsten Durchlauf und lädt die letzten Tage nach.")
    return 0


def cmd_remove_station(settings, _args) -> int:
    items = _station_items(settings)
    if not items:
        print("Keine zusätzlichen Bahnhöfe konfiguriert.")
        return 1
    for i, (eva, label) in enumerate(items, 1):
        print(f"  {i:2}) {label or eva}  (EVA {eva})")
    sel = _ask("Welchen Bahnhof entfernen? (Nummer, mehrere mit Komma): ")
    idx = {int(x) for x in sel.replace(" ", "").split(",") if x.isdigit()}
    keep = [p for i, p in enumerate(items, 1) if i not in idx]
    if len(keep) == len(items):
        print("Nichts geändert.")
        return 0
    value = write_stations(settings.env_path, keep)
    print(f"STATIONS={value}\nGespeicherte Daten bleiben erhalten.")
    return 0


def cmd_search(settings, args) -> int:
    conn = _db(settings)
    hits = search_local(conn, " ".join(args.text), limit=40) if conn else []
    if not hits:
        print("Keine Treffer in den gesammelten Daten.\n" + EVA_HELP)
        return 1
    for h in hits:
        print(f"{h['name']:<42} EVA {h['eva']:<9}{'' if h['stada'] else ' (Bus/Tram)'}")
    return 0


def cmd_status(settings, _args) -> int:
    conn = _db(settings)
    if not conn:
        print(f"Datenbank {settings.db_path} existiert noch nicht.")
        return 1
    st = storage.get_state(conn)
    print(f"Datenbank:        {settings.db_path}")
    for key, label in (("started", "Collector gestartet"), ("last_live", "Letzte Live-Abfrage"),
                       ("last_finalize", "Letzter Abschluss"), ("last_error", "Letzter Fehler")):
        print(f"{label + ':':<18}{st.get(key, '–')}")
    today = dt.datetime.now(TZ).date()
    boards = conn.execute("""SELECT DISTINCT origin_eva eva, 'dep' kind FROM routes WHERE active = 1
                             UNION SELECT DISTINCT dest_eva, 'arr' FROM routes WHERE active = 1""").fetchall()
    done = {(r["board_eva"], r["kind"], r["service_date"]) for r in conn.execute("SELECT * FROM sync_state")}
    missing = 0
    for b in boards:
        for i in range(1, settings.backfill_days + 1):
            if (b["eva"], b["kind"], (today - dt.timedelta(days=i)).isoformat()) not in done:
                missing += 1
    print(f"Offene Tafel-Tage: {missing} (werden automatisch nachgeholt)")
    size = sum(p.stat().st_size for p in Path(settings.db_path).parent.glob(Path(settings.db_path).name + "*"))
    print(f"Datenbankgröße:   {size / 1e6:,.0f} MB")
    tot = conn.execute("SELECT status, COUNT(*) n FROM route_trips GROUP BY status ORDER BY n DESC").fetchall()
    print("Fahrten je Status: " + ", ".join(f"{r['status']} {r['n']}" for r in tot))
    return 0


def cmd_backfill(settings, args) -> int:
    from .collector import Collector
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    col = Collector(settings.env_path)
    col.settings.backfill_days = min(29, args.days)
    col.sync_routes()
    n = col.finalize_pending(interleave_live=False)
    print(f"{n} Tafel-Tage abgeschlossen, {col.client.request_count} Requests.")
    return 0


def cmd_import_db(settings, _args) -> int:
    conn = _db(settings)
    if not conn:
        print("Keine Datenbank gefunden.")
        return 1
    rows = conn.execute("""
        SELECT r.origin_eva, so.name o_name, r.dest_eva, sd.name d_name FROM routes r
        LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
        WHERE r.active = 1 ORDER BY r.id""").fetchall()
    if not rows:
        print("Keine Strecken in der Datenbank.")
        return 1
    pairs = _pairs_from_settings(settings)
    for r in rows:
        if not any(p[0] == r["origin_eva"] and p[2] == r["dest_eva"] for p in pairs):
            pairs.append((r["origin_eva"], r["o_name"] or "", r["dest_eva"], r["d_name"] or ""))
    value = write_routes(settings.env_path, pairs)
    print(f"{len(rows)} Strecken übernommen. ROUTES={value}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dbmon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", type=Path, help="Pfad zur .env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("routes", help="Strecken anzeigen")
    a = sub.add_parser("add", help="Strecke hinzufügen")
    a.add_argument("origin", nargs="?", help="EVA-Nummer Start")
    a.add_argument("dest", nargs="?", help="EVA-Nummer Ziel")
    a.add_argument("--one-way", action="store_true", help="nur diese Richtung")
    sub.add_parser("remove", help="Strecke entfernen")
    sub.add_parser("stations", help="zusätzlich beobachtete Bahnhöfe anzeigen")
    asx = sub.add_parser("add-station", help="Bahnhof ohne Strecke beobachten")
    asx.add_argument("eva", nargs="?", help="EVA-Nummer")
    sub.add_parser("remove-station", help="zusätzlichen Bahnhof entfernen")
    s = sub.add_parser("search", help="Bahnhof in den gesammelten Daten suchen")
    s.add_argument("text", nargs="+")
    sub.add_parser("status", help="Collector-Status")
    b = sub.add_parser("backfill", help="offene Tage sofort abschließen")
    b.add_argument("--days", type=int, default=29)
    sub.add_parser("import-db", help="Strecken aus der Datenbank in die .env übernehmen")
    args = ap.parse_args(argv)
    settings = load_settings(args.env)
    return {
        "routes": cmd_routes, "add": cmd_add, "remove": cmd_remove, "search": cmd_search,
        "status": cmd_status, "stations": cmd_stations, "add-station": cmd_add_station,
        "remove-station": cmd_remove_station, "backfill": cmd_backfill, "import-db": cmd_import_db,
    }[args.cmd](settings, args)


if __name__ == "__main__":
    sys.exit(main())
