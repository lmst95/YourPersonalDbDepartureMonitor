"""Unit tests for config parsing and route classification (edge cases).

Run: python3 -m unittest discover -s tests -v
"""

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dbmonitor import TZ, storage
from dbmonitor.config import format_routes, load_settings, parse_routes, write_routes

DAY = dt.datetime(2026, 11, 18, tzinfo=TZ)
ORIGIN, DEST, DEST_TIEF, OTHER = "8001920", "8000096", "8098096", "8000302"
STOP_PLACES = {
    ORIGIN: {"evaNumber": ORIGIN, "stada": "1716", "localizedNames": [{"name": "Esslingen"}]},
    DEST: {"evaNumber": DEST, "stada": "6071", "localizedNames": [{"name": "Stuttgart Hbf"}]},
    DEST_TIEF: {"evaNumber": DEST_TIEF, "stada": "6071", "localizedNames": [{"name": "Stuttgart Hbf (tief)"}]},
    OTHER: {"evaNumber": OTHER, "stada": "5000", "localizedNames": [{"name": "Plochingen"}]},
}


def entry(jid, hhmm, delay=0, *, via=(DEST,), time_type="REAL", category="S", number=7101, **kw):
    planned = DAY.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))
    e = {
        "journeyId": jid,
        "evaNumber": ORIGIN,
        "timeSchedule": planned.isoformat(),
        "time": (planned + dt.timedelta(minutes=delay)).isoformat(),
        "timeType": time_type,
        "transport": {"category": category, "journeyNumber": number, "type": "CITY_TRAIN",
                      "via": [v if isinstance(v, dict) else {"evaNumber": v} for v in via]},
        "journeyInfo": {"destination": {"evaNumber": "8000999"}, "type": kw.pop("journey_type", "REGULAR")},
        "platformSchedule": {"name": "1"}, "platform": {"name": kw.pop("platform", "1")},
    }
    e.update(kw)
    return e


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        storage.init_db(self.conn)
        storage.upsert_stop_places(self.conn, STOP_PLACES)
        self.conn.execute("INSERT INTO routes (id, origin_eva, dest_eva) VALUES (1, ?, ?)", (ORIGIN, DEST))

    def tearDown(self):
        self.conn.close()

    def load(self, entries, kind="dep", board=ORIGIN, final=True):
        evs = [storage.event_from_entry(e, kind, board, STOP_PLACES, final) for e in entries]
        storage.upsert_events(self.conn, evs)
        storage.reclassify(self.conn, 0, 2**31)
        return {r["ext"]: r for r in self.conn.execute(
            "SELECT t.*, j.ext FROM route_trips t JOIN journeys j ON j.jid = t.jid")}

    def test_ok_and_delay(self):
        t = self.load([entry("j1", "08:00", 4)])
        self.assertEqual(t["j1"]["status"], "ok")
        self.assertEqual(t["j1"]["delay_min"], 4)

    def test_not_serving_destination_is_ignored(self):
        t = self.load([entry("j1", "08:00", via=(OTHER,))])
        self.assertEqual(t, {})

    def test_station_group_hbf_tief(self):
        t = self.load([entry("j1", "08:00", via=(DEST_TIEF,))])
        self.assertIn("j1", t)

    def test_destination_only_in_journey_info(self):
        e = entry("j1", "08:00", via=(OTHER,))
        e["journeyInfo"]["destination"] = {"evaNumber": DEST}
        self.assertEqual(self.load([e])["j1"]["status"], "ok")

    def test_cancelled(self):
        t = self.load([entry("j1", "08:00", cancelled=True, time_type="SCHEDULE")])
        self.assertEqual(t["j1"]["status"], "cancelled")
        self.assertIsNone(t["j1"]["delay_min"])

    def test_cancelled_additional_stop_ignored(self):
        t = self.load([entry("j1", "08:00", cancelled=True, additional=True)])
        self.assertEqual(t, {})

    def test_partial_destination_cancelled(self):
        t = self.load([entry("j1", "08:00", 2, via=({"evaNumber": DEST, "cancelled": True},))])
        self.assertEqual(t["j1"]["status"], "partial")

    def test_partial_via_arrival_board(self):
        self.load([entry("j1", "08:00", 2)])
        arr = entry("j1", "08:20", cancelled=True)
        arr["evaNumber"] = DEST
        t = self.load([arr], kind="arr", board=DEST)
        self.assertEqual(t["j1"]["status"], "partial")

    def test_arrival_delay(self):
        self.load([entry("j1", "08:00", 2)])
        arr = entry("j1", "08:20", 7)
        t = self.load([arr], kind="arr", board=DEST)
        self.assertEqual(t["j1"]["arr_delay_min"], 7)

    def test_replacement_train(self):
        orig = entry("orig", "08:00", cancelled=True, time_type="SCHEDULE",
                     replacedBy=[{"journeyID": "repl", "category": "RE", "journeyNumber": 32576}])
        repl = entry("repl", "08:00", 19, number=32576, category="RE", journey_type="REPLACEMENT",
                     replacementFor=[{"journeyID": "orig"}])
        t = self.load([orig, repl])
        self.assertEqual(t["orig"]["status"], "replaced")
        self.assertEqual(t["orig"]["delay_min"], 19)
        self.assertEqual(t["orig"]["replacement_label"], "RE 32576")
        self.assertEqual(t["repl"]["status"], "replacement")

    def test_replaced_by_bus_is_sev(self):
        orig = entry("orig", "08:00", cancelled=True, replacedBy=[{"journeyID": "bus"}])
        bus = entry("bus", "08:05", category="Bus", replacementFor=[{"journeyID": "orig"}],
                    transport_extra=None)
        bus["transport"]["replacementTransport"] = {"realType": "BUS"}
        bus["timeType"] = "SCHEDULE"
        t = self.load([orig, bus])
        self.assertEqual(t["orig"]["status"], "sev")
        self.assertEqual(t["bus"]["status"], "replacement")

    def test_standalone_sev_bus(self):
        bus = entry("bus", "08:05", category="Bus", time_type="SCHEDULE")
        bus["transport"]["replacementTransport"] = {"realType": "BUS"}
        self.assertEqual(self.load([bus])["bus"]["status"], "sev")

    def test_no_real_time_is_unknown_when_final(self):
        t = self.load([entry("j1", "08:00", 3, time_type="PREVIEW")])
        self.assertEqual(t["j1"]["status"], "unknown")
        self.assertIsNone(t["j1"]["delay_min"])

    def test_live_preview_is_ok_but_not_real(self):
        t = self.load([entry("j1", "08:00", 3, time_type="PREVIEW")], final=False)
        self.assertEqual(t["j1"]["status"], "ok")
        self.assertEqual(t["j1"]["time_type"], "PREVIEW")

    def test_no_passenger_change_counts_as_cancelled(self):
        t = self.load([entry("j1", "08:00", noPassengerChange=True)])
        self.assertEqual(t["j1"]["status"], "cancelled")

    def test_extra_and_platform_change_flags(self):
        t = self.load([entry("j1", "08:00", journey_type="RELIEF", platform="3")])
        self.assertEqual(t["j1"]["extra"], 1)
        self.assertEqual(t["j1"]["platform_changed"], 1)

    def test_coupled_group(self):
        e = entry("b-part", "08:00", travelsWith=[{"journeyID": "a-part"}])
        t = self.load([e])["b-part"]
        self.assertEqual(self.conn.execute("SELECT ext FROM journeys WHERE jid = ?", (t["coupled_jid"],)).fetchone()[0], "a-part")

    def test_final_not_overwritten_by_live(self):
        self.load([entry("j1", "08:00", 5)], final=True)
        t = self.load([entry("j1", "08:00", 1, time_type="PREVIEW")], final=False)
        self.assertEqual(t["j1"]["delay_min"], 5)


class LegacyMigrationTest(unittest.TestCase):
    def test_migrates_and_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            c = sqlite3.connect(p)
            c.executescript("""
                CREATE TABLE routes (id INTEGER PRIMARY KEY, origin_name TEXT, dest_name TEXT, origin_eva TEXT,
                    dest_eva TEXT, origin_lat REAL, origin_lon REAL, dest_lat REAL, dest_lon REAL);
                CREATE TABLE departures (id INTEGER PRIMARY KEY, route_id INT, service_id TEXT, category TEXT,
                    number TEXT, planned_dt TEXT, realtime_dt TEXT, delay_min INT, planned_platform TEXT,
                    realtime_platform TEXT, inserted_at TEXT, status TEXT);
                INSERT INTO routes VALUES (1,'Esslingen','Stuttgart Hbf','8001920','8000096',48.7,9.3,48.8,9.2);
                INSERT INTO routes VALUES (2,'Esslingen','Plochingen','8001920','8000302',48.7,9.3,48.7,9.4);
                INSERT INTO departures VALUES (1,1,'s1','S','7101','2026-11-01T08:00:00+01:00','2026-11-01T08:03:00+01:00',3,'1','1',NULL,NULL);
                INSERT INTO departures VALUES (2,2,'s1','S','7101','2026-11-01T08:00:00+01:00','2026-11-01T08:03:00+01:00',3,'1','1',NULL,NULL);
                INSERT INTO departures VALUES (3,1,'s2','S','7103','2026-11-01T08:15:00+01:00',NULL,12,'1','1',NULL,'c');
                INSERT INTO departures VALUES (4,1,'s3','S','7105','2026-11-01T08:30:00+01:00',NULL,0,'1','1',NULL,NULL);
            """)
            c.commit()
            c.close()
            conn = storage.connect(str(p))
            storage.init_db(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stop_events").fetchone()[0], 3)
            st = dict(conn.execute("SELECT j.ext, t.status FROM route_trips t JOIN journeys j ON j.jid = t.jid "
                                   "WHERE route_id = 1").fetchall())
            self.assertEqual(st, {"iris:s1": "ok", "iris:s2": "cancelled", "iris:s3": "unknown"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM departures_legacy").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT name FROM stations WHERE eva='8000096'").fetchone()[0], "Stuttgart Hbf")
            conn.close()


class ConfigTest(unittest.TestCase):
    def test_parse(self):
        routes, errors = parse_routes("8001920(Esslingen)<->8000096(Stuttgart Hbf); 8001920->8000302;Berlin Hbf->Hamburg Hbf;kaputt")
        self.assertEqual(len(routes), 4)
        self.assertEqual(routes[0].origin.eva, "8001920")
        self.assertEqual(routes[0].origin.label, "Esslingen")
        self.assertEqual(routes[1].origin.eva, "8000096")
        self.assertEqual(routes[3].origin.name, "Berlin Hbf")
        self.assertEqual(len(errors), 1)

    def test_format_merges_directions(self):
        v = format_routes([("1", "A", "2", "B"), ("2", "B", "1", "A"), ("1", "A", "3", "C")])
        self.assertEqual(v, "1(A)<->2(B);1(A)->3(C)")

    def test_write_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# Kommentar\nDB_CLIENT_ID=x\nPOLLING_ROUTES=A->B\nDB_API_KEY=y\n")
            write_routes(p, [("8001920", "Ess(lingen);", "8000096", "Stgt")])
            text = p.read_text()
            self.assertIn("DB_CLIENT_ID=x", text)
            self.assertIn("# Kommentar", text)
            self.assertIn("ROUTES=8001920(Ess lingen)->8000096(Stgt)", text)
            self.assertIn("# POLLING_ROUTES=A->B", text)
            self.assertTrue((Path(d) / ".env.bak").exists())
            s = load_settings(p)
            self.assertEqual([(r.origin.eva, r.dest.eva) for r in s.routes], [("8001920", "8000096")])


if __name__ == "__main__":
    unittest.main()


class FakeClient:
    """Returns prepared entries whose best-known time lies inside the requested window (like OpenBoards)."""

    def __init__(self, entries):
        self.entries = entries
        self.request_count = 0

    def board_range(self, kind, eva, start, end):
        self.request_count += 1
        out = []
        for e in self.entries:
            t = dt.datetime.fromisoformat(e["time"])
            if start <= t < end:
                out.append(json.loads(json.dumps(e)))
        return out, {"stopPlaces": STOP_PLACES, "causeCodes": {}}


class LateTrainTest(unittest.TestCase):
    """A train planned 19:00 that departs 10 h late (05:00 next day) must not get lost."""

    def setUp(self):
        from dbmonitor.collector import Collector
        self.conn = storage.connect(":memory:")
        storage.init_db(self.conn)
        storage.upsert_stop_places(self.conn, STOP_PLACES)
        self.conn.execute("INSERT INTO routes (id, origin_eva, dest_eva) VALUES (1, ?, ?)", (ORIGIN, DEST))
        self.col = Collector.__new__(Collector)
        self.col.conn = self.conn
        self.late = entry("late", "19:00", 600)          # planned DAY 19:00, departs DAY+1 05:00
        self.normal = entry("normal", "18:00", 2)

    def tearDown(self):
        self.conn.close()

    def finalize(self, day, now):
        start = dt.datetime.combine(day, dt.time(0), TZ)
        end = start + dt.timedelta(days=1)
        return self.col.sync_board(ORIGIN, "dep", start, min(end + dt.timedelta(hours=4), now), start, end, final=True)

    def trips(self):
        return {r["ext"]: r for r in self.conn.execute(
            "SELECT t.*, j.ext FROM route_trips t JOIN journeys j ON j.jid = t.jid")}

    def test_late_train_recorded_by_next_days_finalization(self):
        self.col.client = FakeClient([self.late, self.normal])
        d0 = DAY.date()
        # finalization of DAY at 03:00 next day: the late train has not left yet -> not in this board
        self.finalize(d0, DAY + dt.timedelta(hours=27))
        self.assertNotIn("late", self.trips())
        self.assertEqual(self.trips()["normal"]["status"], "ok")
        # finalization of DAY+1 at 03:00 the day after: the straggler is in this board and is kept
        self.finalize(d0 + dt.timedelta(days=1), DAY + dt.timedelta(hours=51))
        t = self.trips()["late"]
        self.assertEqual((t["status"], t["delay_min"], t["final"]), ("ok", 600, 1))
        self.assertTrue(storage.iso_local(t["planned"]).startswith("2026-11-18T19:00"))

    def test_live_keeps_straggler_and_final_run_does_not_delete_it(self):
        from dbmonitor.collector import LIVE_BACK, LATE_MARGIN
        late_preview = dict(self.late, timeType="PREVIEW")
        self.col.client = FakeClient([late_preview])
        now = DAY + dt.timedelta(hours=28, minutes=30)   # 04:30 next day, predicted departure 05:00
        self.col.sync_board(ORIGIN, "dep", now - LIVE_BACK, now + dt.timedelta(minutes=90),
                            now - LIVE_BACK - LATE_MARGIN, now + dt.timedelta(minutes=90), final=False)
        self.assertEqual(self.trips()["late"]["time_type"], "PREVIEW")
        # a final run for DAY whose window ends before the predicted time must not delete the live row
        self.col.client = FakeClient([])
        self.finalize(DAY.date(), DAY + dt.timedelta(hours=27))
        self.assertIn("late", self.trips())


class StationViewTest(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        storage.init_db(self.conn)
        storage.upsert_stop_places(self.conn, STOP_PLACES)
        self.conn.execute("INSERT INTO routes (id, origin_eva, dest_eva) VALUES (1, ?, ?)", (ORIGIN, DEST))

    def tearDown(self):
        self.conn.close()

    def load(self, entries, final=True):
        evs = [storage.event_from_entry(e, "dep", ORIGIN, STOP_PLACES, final) for e in entries]
        storage.upsert_events(self.conn, evs)
        storage.classify_station(self.conn, ORIGIN, "dep", 0, 2**31)
        return {r["ext"]: r for r in self.conn.execute(
            "SELECT e.*, j.ext FROM stop_events e JOIN journeys j ON j.jid = e.jid")}

    def test_all_trains_counted_not_only_route(self):
        ev = self.load([entry("other", "08:00", 7, via=(OTHER,))])  # does not serve the route destination
        self.assertEqual((ev["other"]["st_status"], ev["other"]["st_delay"], ev["other"]["st_real"]), ("ok", 7, 1))

    def test_statuses(self):
        bus = entry("bus", "09:05", category="Bus", time_type="SCHEDULE", replacementFor=[{"journeyID": "c2"}])
        bus["transport"]["replacementTransport"] = {"realType": "BUS"}
        ev = self.load([
            entry("c1", "08:00", cancelled=True),
            entry("x", "08:10", cancelled=True, additional=True),
            entry("orig", "08:20", cancelled=True, replacedBy=[{"journeyID": "repl"}]),
            entry("repl", "08:20", 12, replacementFor=[{"journeyID": "orig"}]),
            entry("c2", "09:00", cancelled=True, replacedBy=[{"journeyID": "bus"}]),
            bus,
            entry("u", "10:00", 3, time_type="PREVIEW"),
        ])
        st = {k: v["st_status"] for k, v in ev.items()}
        self.assertEqual(st, {"c1": "cancelled", "x": "ignore", "orig": "replaced", "repl": "replacement",
                              "c2": "sev", "bus": "replacement", "u": "unknown"})
        self.assertEqual(ev["orig"]["st_delay"], 12)

    def test_route_geometry_prefers_detailed_common_pattern(self):
        a = entry("s1", "08:00", via=("8001921", "8006643", DEST))      # S-Bahn, more stops
        b = entry("re", "08:05", via=("8005769", DEST))                  # express, few stops
        c = entry("s1b", "08:15", via=("8001921", "8006643", DEST))
        d = entry("re2", "08:20", via=("8005769", DEST))
        e = entry("re3", "08:25", via=("8005769", DEST))
        evs = [storage.event_from_entry(x, "dep", ORIGIN, STOP_PLACES, True) for x in (a, b, c, d, e)]
        storage.upsert_events(self.conn, evs)
        storage.reclassify(self.conn, 0, 2**31)
        self.assertEqual(storage.route_geometry(self.conn, 1), [ORIGIN, "8001921", "8006643", DEST])

    def test_parse_stations(self):
        from dbmonitor.config import parse_stations, format_stations
        refs, errors = parse_stations("8006642(Esslingen-Zell); 8005769;8006642")
        self.assertEqual([r.eva for r in refs], ["8006642", "8005769"])
        self.assertEqual(errors, [])
        self.assertEqual(format_stations([("8006642", "Esslingen-Zell"), ("8005769", "")]), "8006642(Esslingen-Zell);8005769")


class EncodingTest(unittest.TestCase):
    def test_path_roundtrip_with_overrides(self):
        stadas = {"1": "10", "2": "20", "3": None}
        path = [{"e": "1", "s": "10"}, {"e": "2", "s": "99", "x": 1}, {"e": "3", "s": None, "a": 1},
                {"e": "4", "s": "40"}, {"e": "1", "s": None, "x": 1, "a": 1}, {"e": None, "s": None}]
        enc = storage.encode_path(path, stadas)
        self.assertEqual(storage.decode_path(enc, stadas), path)
        self.assertEqual(storage.decode_path(storage.encode_path([], stadas), stadas), [])
        self.assertIsNone(storage.decode_path(storage.encode_path(None, stadas), stadas))

    def test_times_and_stop_eva(self):
        for iso in ("2026-11-18T19:00:00+01:00", "2026-07-01T23:59:59+02:00", "2026-03-29T03:00:00+02:00"):
            self.assertEqual(storage.iso_local(storage.to_epoch(iso)), iso)
        for stop in ("8000096", "8098096", None):
            self.assertEqual(storage.decode_stop_eva(storage.encode_stop_eva(stop, "8000096"), "8000096"), stop)


class AggregateConsistencyTest(unittest.TestCase):
    """KPIs from the daily aggregates must equal the KPIs computed from raw rows."""

    def test_aggregates_equal_raw(self):
        import dbmonitor.web as w
        conn = storage.connect(":memory:")
        storage.init_db(conn)
        storage.upsert_stop_places(conn, STOP_PLACES)
        conn.execute("INSERT INTO routes (id, origin_eva, dest_eva) VALUES (1, ?, ?)", (ORIGIN, DEST))
        entries = []
        for day in range(3):
            for i, (hh, delay) in enumerate((("06:10", 2), ("07:20", 9), ("17:45", 31), ("23:50", 0))):
                e = entry(f"d{day}-{i}", hh, delay, number=7100 + i, cause_codes=None)
                e["timeSchedule"] = (dt.datetime.fromisoformat(e["timeSchedule"]) + dt.timedelta(days=day)).isoformat()
                e["time"] = (dt.datetime.fromisoformat(e["time"]) + dt.timedelta(days=day)).isoformat()
                e["_causeCodes"] = ["43"] if delay >= 6 else []
                entries.append(e)
        entries.append(entry("cx", "12:00", cancelled=True))
        evs = [storage.event_from_entry(e, "dep", ORIGIN, STOP_PLACES, True) for e in entries]
        storage.upsert_events(conn, evs)
        storage.reclassify(conn, 0, 2**31)
        storage.classify_station(conn, ORIGIN, "dep", 0, 2**31)
        start, end = storage.day_bounds(DAY.date())[0], storage.day_bounds(DAY.date() + dt.timedelta(days=2))[1]
        raw_r = w.kpis_of(storage.aggregate(storage.trip_rows(conn, "r", "1", start, end)))
        raw_d = w.kpis_of(storage.aggregate(storage.trip_rows(conn, "d", ORIGIN, start, end)))
        for i in range(3):
            d = DAY.date() + dt.timedelta(days=i)
            storage.write_aggregates(conn, "r", "1", d)
            storage.write_aggregates(conn, "d", ORIGIN, d)
        self.assertEqual(w.kpis_fast(conn, "r", "1", start, end), raw_r)
        self.assertEqual(w.kpis_of(w.collect(conn, "r", "1", start, end)), raw_r)
        self.assertEqual(w.kpis_fast(conn, "d", ORIGIN, start, end), raw_d)
        self.assertEqual(raw_r["planned"], 13)
        self.assertEqual(raw_r["cancelled"], 1)
        agg = w.collect(conn, "d", ORIGIN, start, end)
        self.assertEqual(sum(agg["cause"].values()), 6)
        conn.close()
