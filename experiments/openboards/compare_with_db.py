"""
Compare delays stored in the existing SQLite DB (Timetables/IRIS based) with what
OpenBoards reports for the same departures.

OpenBoards only serves the last 30 days, so only DB rows from that range are compared.
The DB is opened read-only and never modified.

Usage:
    python experiments/openboards/compare_with_db.py --db ./train_db.db --days 7
    python experiments/openboards/compare_with_db.py --db ./train_db.db --days 3 --route-id 2 --csv out.csv

Output: per route the number of matched departures, departures only present in one source,
and the distribution of delay differences (OpenBoards minus DB).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openboards_client import (  # noqa: E402
    MAX_HISTORY_DAYS, TZ, OpenBoardsClient, delay_minutes, parse_ts, serves,
)

Key = Tuple[str, dt.datetime]  # (train number, planned departure in UTC)
LATE_MARGIN = dt.timedelta(hours=4)


def db_rows(conn: sqlite3.Connection, route_id: int, since: dt.datetime, until: dt.datetime) -> List[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM departures WHERE route_id = ? ORDER BY planned_dt", (route_id,)
    ).fetchall()
    out = []
    for r in rows:
        p = dt.datetime.fromisoformat(r["planned_dt"])
        if p.tzinfo is None:
            p = p.replace(tzinfo=TZ)
        if since <= p < until:
            out.append(r)
    return out


def key_db(r: sqlite3.Row) -> Key:
    p = dt.datetime.fromisoformat(r["planned_dt"])
    if p.tzinfo is None:
        p = p.replace(tzinfo=TZ)
    return (str(r["number"] or "").strip(), p.astimezone(dt.timezone.utc).replace(second=0))


def key_ob(e: Dict[str, Any]) -> Key:
    tr = e.get("transport") or {}
    p = parse_ts(e["timeSchedule"])
    return (str(tr.get("journeyNumber") or "").strip(), p.astimezone(dt.timezone.utc).replace(second=0))


def compare_route(client: OpenBoardsClient, conn: sqlite3.Connection, route: sqlite3.Row,
                  since: dt.datetime, until: dt.datetime, csv_writer=None) -> None:
    rows = db_rows(conn, route["id"], since, until)
    print(f"\n### Route {route['id']}: {route['origin_name']} ({route['origin_eva']}) -> "
          f"{route['dest_name']} ({route['dest_eva']}), {len(rows)} DB-Einträge im Zeitraum")
    if not rows:
        return

    # Only query the days that actually have DB data (saves requests).
    days = sorted({dt.datetime.fromisoformat(r["planned_dt"]).astimezone(TZ).date() for r in rows})
    ob: Dict[Key, Dict[str, Any]] = {}
    ob_all_origin: Dict[Key, Dict[str, Any]] = {}
    for day in days:
        day_start = dt.datetime.combine(day, dt.time(0), TZ)
        start = max(day_start, since)
        end = min(day_start + dt.timedelta(days=1), until)
        if start >= end:
            continue
        # The board's time window filters on the *actual* time, not the schedule. Query a few
        # extra hours so late trains scheduled near the end of the day are included, then keep
        # only entries whose scheduled time lies inside [start, end).
        query_end = min(end + LATE_MARGIN, dt.datetime.now(TZ))
        for e in client.board_range("departures", route["origin_eva"], start, query_end):
            sched = parse_ts(e.get("timeSchedule"))
            if not sched or not (start <= sched < end):
                continue
            k = key_ob(e)
            ob_all_origin[k] = e
            if serves(e, route["dest_eva"]):
                ob[k] = e

    db = {key_db(r): r for r in rows}
    matched = db.keys() & ob.keys()
    only_db = db.keys() - ob.keys()
    only_ob = ob.keys() - db.keys()
    # DB entries that exist in OpenBoards at the origin but fail our destination filter
    filter_miss = [k for k in only_db if k in ob_all_origin]

    diffs: List[int] = []
    cancel_mismatch = 0
    timetypes = Counter()
    for k in sorted(matched, key=lambda k: k[1]):
        r, e = db[k], ob[k]
        timetypes[e.get("timeType")] += 1
        db_cancel = r["status"] == "c" if "status" in r.keys() else False
        ob_cancel = bool(e.get("cancelled"))
        if db_cancel != ob_cancel:
            cancel_mismatch += 1
        d_ob = delay_minutes(e)
        d_db = r["delay_min"]
        if d_ob is not None and d_db is not None and not (db_cancel or ob_cancel):
            diffs.append(d_ob - d_db)
        if csv_writer:
            csv_writer.writerow({
                "route_id": route["id"], "train": f"{r['category']} {r['number']}",
                "planned": k[1].astimezone(TZ).isoformat(timespec="minutes"),
                "db_delay": d_db, "ob_delay": d_ob, "ob_timeType": e.get("timeType"),
                "db_status": r["status"] if "status" in r.keys() else None,
                "ob_cancelled": ob_cancel, "ob_journeyId": e.get("journeyId"),
            })

    print(f"  gematcht: {len(matched)} | nur DB: {len(only_db)} (davon {len(filter_miss)} am Start "
          f"vorhanden, aber Zielfilter greift nicht) | nur OpenBoards: {len(only_ob)}")
    print(f"  timeType der Matches: {dict(timetypes)} | Ausfall-Abweichungen: {cancel_mismatch}")
    if diffs:
        c = Counter(diffs)
        exact = c.get(0, 0)
        within1 = sum(v for d, v in c.items() if abs(d) <= 1)
        print(f"  Verspätungsdifferenz OB-DB (min): exakt gleich {exact}/{len(diffs)} "
              f"({100*exact/len(diffs):.0f}%), |diff|<=1: {100*within1/len(diffs):.0f}%, "
              f"Median {statistics.median(diffs)}, min {min(diffs)}, max {max(diffs)}")
        big = sorted((d for d in diffs if abs(d) > 1), key=abs, reverse=True)[:10]
        if big:
            print(f"  größte Abweichungen: {big}")
    for label, keys in (("nur DB", only_db), ("nur OpenBoards", only_ob)):
        sample = sorted(keys, key=lambda k: k[1])[:5]
        if sample:
            print(f"  Beispiele {label}: " + ", ".join(f"{n} {t.astimezone(TZ):%d.%m %H:%M}" for n, t in sample))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="Pfad zur SQLite-Datenbank (wird nur gelesen)")
    ap.add_argument("--days", type=int, default=7, help=f"Wie viele Tage zurück (max {MAX_HISTORY_DAYS})")
    ap.add_argument("--route-id", type=int, action="append", help="Nur diese Route(n)")
    ap.add_argument("--csv", help="Alle Matches als CSV schreiben")
    ap.add_argument("--no-raw", action="store_true", help="Rohantworten nicht speichern")
    args = ap.parse_args()

    days = min(args.days, MAX_HISTORY_DAYS - 1)  # keep a safety margin at the 30-day edge
    until = dt.datetime.now(TZ).replace(second=0, microsecond=0) - dt.timedelta(hours=1)
    since = until - dt.timedelta(days=days)

    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    routes = conn.execute("SELECT * FROM routes ORDER BY id").fetchall()
    if args.route_id:
        routes = [r for r in routes if r["id"] in args.route_id]

    client = OpenBoardsClient(save_raw=not args.no_raw)
    writer = None
    fh = None
    if args.csv:
        fh = open(args.csv, "w", newline="")
        writer = csv.DictWriter(fh, fieldnames=["route_id", "train", "planned", "db_delay", "ob_delay",
                                                "ob_timeType", "db_status", "ob_cancelled", "ob_journeyId"])
        writer.writeheader()

    print(f"Zeitraum: {since:%d.%m.%Y %H:%M} – {until:%d.%m.%Y %H:%M}, {len(routes)} Routen")
    try:
        for route in routes:
            compare_route(client, conn, route, since, until, writer)
    finally:
        if fh:
            fh.close()
    print(f"\n{client.request_count} OpenBoards-Requests")


if __name__ == "__main__":
    main()
