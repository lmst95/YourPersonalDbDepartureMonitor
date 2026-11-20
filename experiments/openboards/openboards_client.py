"""
Minimal client + probe for the DB OpenBoards API (successor of the Timetables/IRIS API).

Spec: https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards
Endpoint: POST {BASE}/departures and POST {BASE}/arrivals (JSON body, JSON response)

Credentials: same DB_CLIENT_ID / DB_API_KEY as the Timetables API (read from the repo's .env),
but the app must additionally be subscribed to the OpenBoards "Free" plan in the marketplace.

Usage:
    python experiments/openboards/openboards_client.py probe --origin 8000105 --dest 8000191
    python experiments/openboards/openboards_client.py board --eva 8000105 --start 2026-11-18T07:00 --hours 1

All raw responses are written to experiments/openboards/out/ (git-ignored) for inspection.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import requests

BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/open-boards/v1"
TZ = ZoneInfo("Europe/Berlin")
TIMEOUT = 30
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent / "out"

# API limits (from the OpenAPI spec)
MAX_WINDOW = dt.timedelta(hours=8)
MAX_ENTRIES_PER_TYPE = 50
MAX_HISTORY_DAYS = 30
MIN_REQUEST_INTERVAL = 0.12  # free plan: 10 requests / second


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class OpenBoardsError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class OpenBoardsClient:
    def __init__(self, client_id: Optional[str] = None, api_key: Optional[str] = None, save_raw: bool = True):
        load_env()
        self.client_id = client_id or os.environ.get("DB_CLIENT_ID")
        self.api_key = api_key or os.environ.get("DB_API_KEY")
        if not self.client_id or not self.api_key:
            sys.exit("DB_CLIENT_ID / DB_API_KEY fehlen (in .env im Repo-Root eintragen).")
        self.session = requests.Session()
        self.session.headers.update({
            "DB-Client-Id": self.client_id,
            "DB-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "db-live-pipeline/openboards-probe",
        })
        self.save_raw = save_raw
        self._last_request = 0.0
        self.request_count = 0

    # -------------------------------------------------------------- raw calls
    def _post(self, path: str, body: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        for attempt in range(retries + 1):
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            self.request_count += 1
            try:
                r = self.session.post(f"{BASE}{path}", json=body, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout):
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
            if not r.ok:
                raise OpenBoardsError(r.status_code, r.text)
            data = r.json()
            for sp in data.get("stopPlaces") or []:
                if sp.get("evaNumber") and sp.get("stada"):
                    STADA_BY_EVA[sp["evaNumber"]] = sp["stada"]
            if self.save_raw:
                self._dump(path, body, data)
            return data
        raise AssertionError("unreachable")

    def _dump(self, path: str, body: Dict[str, Any], data: Dict[str, Any]) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        sp = body.get("stopPlaceOrStation", {})
        ident = "-".join(sp.get("evaNumbers") or [sp.get("stada") or "x"])
        start = (body.get("timeWindow") or {}).get("timeStart", "now").replace(":", "")
        fname = OUT_DIR / f"{path.strip('/')}_{ident}_{start}.json"
        fname.write_text(json.dumps({"request": body, "response": data}, ensure_ascii=False, indent=1))

    def board(
        self,
        kind: str,
        eva: str,
        start: Optional[dt.datetime] = None,
        end: Optional[dt.datetime] = None,
        max_via_stops: Optional[int] = 100,
        max_entries: int = MAX_ENTRIES_PER_TYPE,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """kind: 'departures' or 'arrivals'."""
        body: Dict[str, Any] = {
            "stopPlaceOrStation": {"evaNumbers": [eva]},
            "maxEntriesPerTransportType": max_entries,
            "languages": ["deu"],
        }
        if max_via_stops is not None:
            body["maxViaStops"] = max_via_stops
        tw: Dict[str, str] = {}
        if start:
            tw["timeStart"] = start.isoformat(timespec="seconds")
        if end:
            tw["timeEnd"] = end.isoformat(timespec="seconds")
        if tw:
            body["timeWindow"] = tw
        if extra:
            body.update(extra)
        return self._post(f"/{kind}", body)

    def board_range(self, kind: str, eva: str, start: dt.datetime, end: dt.datetime,
                    step: dt.timedelta = dt.timedelta(hours=2), **kw) -> List[Dict[str, Any]]:
        """Fetch [start, end) in windows; windows whose result is truncated are split in half.
        Results are cached per (kind, eva, start, end) so routes sharing an origin reuse them."""
        cache_key = (kind, eva, start, end, step)
        cache = self.__dict__.setdefault("_range_cache", {})
        if cache_key in cache:
            return cache[cache_key]
        entries: Dict[str, Dict[str, Any]] = {}
        key = "departures" if kind == "departures" else "arrivals"
        stack = []
        t = start
        while t < end:
            stack.append((t, min(t + step, end)))
            t += step
        stack.reverse()
        while stack:
            a, b = stack.pop()
            data = self.board(kind, eva, a, b, **kw)
            truncated = (data.get("meta") or {}).get("truncatedTransportTypes") or []
            if truncated and (b - a) > dt.timedelta(minutes=10):
                mid = a + (b - a) / 2
                stack.extend([(mid, b), (a, mid)])
                continue
            if truncated:
                print(f"  ! Fenster {a:%H:%M}-{b:%H:%M} trotz Teilung abgeschnitten: {truncated}", file=sys.stderr)
            for e in data.get(key) or []:
                uid = f"{e.get('journeyId')}|{e.get('departureId') or e.get('arrivalId')}|{e.get('timeSchedule')}"
                entries[uid] = e
        cache[cache_key] = list(entries.values())
        return cache[cache_key]


# ------------------------------------------------------------------ helpers
def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def delay_minutes(entry: Dict[str, Any]) -> Optional[int]:
    sched, best = parse_ts(entry.get("timeSchedule")), parse_ts(entry.get("time"))
    if not sched or not best:
        return None
    return int((best - sched).total_seconds() // 60)


def via_evas(entry: Dict[str, Any]) -> List[str]:
    tr = entry.get("transport") or {}
    return [v.get("evaNumber") for v in tr.get("via") or [] if v.get("evaNumber")]


# eva -> stada (station id), filled from every response's stopPlaces. Stop places of the same
# station share a stada, e.g. "Stuttgart Hbf" (8000096) and "Stuttgart Hbf (tief)" (8098096).
STADA_BY_EVA: Dict[str, str] = {}


def same_station(eva_a: Optional[str], eva_b: str) -> bool:
    if not eva_a:
        return False
    if eva_a == eva_b:
        return True
    sa, sb = STADA_BY_EVA.get(eva_a), STADA_BY_EVA.get(eva_b)
    return sa is not None and sa == sb


def serves(entry: Dict[str, Any], dest_eva: str) -> bool:
    """True if dest (or another stop place of the same station) is a non-cancelled via stop
    or the final destination of the journey."""
    tr = entry.get("transport") or {}
    for v in tr.get("via") or []:
        if same_station(v.get("evaNumber"), dest_eva) and not v.get("cancelled"):
            return True
    dest = ((entry.get("journeyInfo") or {}).get("destination") or {})
    return same_station(dest.get("evaNumber"), dest_eva)


def describe(entry: Dict[str, Any]) -> str:
    tr = entry.get("transport") or {}
    name = f"{tr.get('category') or ''} {tr.get('journeyNumber') or ''}".strip()
    sched = parse_ts(entry.get("timeSchedule"))
    d = delay_minutes(entry)
    flags = []
    if entry.get("cancelled"):
        flags.append("AUSFALL")
    if entry.get("additional"):
        flags.append("zusätzlich")
    plat = (entry.get("platform") or {}).get("name")
    plat_s = (entry.get("platformSchedule") or {}).get("name")
    return (f"{sched.astimezone(TZ):%d.%m %H:%M} {name:<10} +{d if d is not None else '?':>3} "
            f"[{entry.get('timeType')}] Gl {plat_s}->{plat} {' '.join(flags)}")


# ------------------------------------------------------------------- probe
def probe(client: OpenBoardsClient, origin: str, dest: Optional[str]) -> None:
    now = dt.datetime.now(TZ).replace(second=0, microsecond=0)
    checks = [
        ("jetzt (Default-Fenster, ohne timeWindow)", None, None),
        ("letzte Stunde", now - dt.timedelta(hours=1), now),
        ("gestern, gleiche Stunde", now - dt.timedelta(days=1, hours=1), now - dt.timedelta(days=1)),
        ("vor 29 Tagen", now - dt.timedelta(days=29, hours=1), now - dt.timedelta(days=29)),
        ("vor 31 Tagen (sollte leer/Fehler sein)", now - dt.timedelta(days=31, hours=1), now - dt.timedelta(days=31)),
        ("8h-Fenster gestern", now - dt.timedelta(days=1, hours=8), now - dt.timedelta(days=1)),
    ]
    for label, a, b in checks:
        print(f"\n=== {label} ===")
        try:
            data = client.board("departures", origin, a, b)
        except OpenBoardsError as e:
            print(f"  Fehler: {e}")
            continue
        deps = data.get("departures") or []
        meta = data.get("meta") or {}
        print(f"  {len(deps)} Abfahrten, truncated={meta.get('truncatedTransportTypes')}, "
              f"timeType={dict(Counter(d.get('timeType') for d in deps))}")
        via_lens = [len(via_evas(d)) for d in deps]
        if via_lens:
            print(f"  via-Halte pro Fahrt: min={min(via_lens)} max={max(via_lens)}")
        sel = [d for d in deps if serves(d, dest)] if dest else deps
        if dest:
            print(f"  davon nach {dest}: {len(sel)}")
        for d in sel[:8]:
            print("   ", describe(d))

    print("\n=== maxViaStops-Test ===")
    for n in (None, 3, 100, 1000):
        try:
            data = client.board("departures", origin, now - dt.timedelta(hours=1), now, max_via_stops=n)
            lens = [len(via_evas(d)) for d in data.get("departures") or []]
            print(f"  maxViaStops={n}: max via={max(lens) if lens else '-'}")
        except OpenBoardsError as e:
            print(f"  maxViaStops={n}: Fehler {e.status}: {e.body[:200]}")

    if dest:
        print("\n=== Ankünfte am Ziel (letzte 2h) ===")
        try:
            data = client.board("arrivals", dest, now - dt.timedelta(hours=2), now)
            arr = data.get("arrivals") or []
            print(f"  {len(arr)} Ankünfte, timeType={dict(Counter(a.get('timeType') for a in arr))}")
            for a in arr[:5]:
                print("   ", describe(a))
        except OpenBoardsError as e:
            print(f"  Fehler: {e}")

    print(f"\n{client.request_count} Requests. Rohdaten in {OUT_DIR}")


def main(argv: Optional[Iterable[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="Grundlegende Testcalls (Historie, Limits, via-Halte)")
    p.add_argument("--origin", required=True, help="EVA-Nummer Start, z.B. 8000105 (Frankfurt Hbf)")
    p.add_argument("--dest", help="EVA-Nummer Ziel, z.B. 8000191 (Karlsruhe Hbf)")
    b = sub.add_parser("board", help="Einzelne Tafel abfragen")
    b.add_argument("--eva", required=True)
    b.add_argument("--kind", choices=["departures", "arrivals"], default="departures")
    b.add_argument("--start", help="lokale Zeit, z.B. 2026-11-18T07:00")
    b.add_argument("--hours", type=float, default=1.0)
    b.add_argument("--dest", help="nur Fahrten zu dieser EVA anzeigen")
    args = ap.parse_args(argv)

    client = OpenBoardsClient()
    if args.cmd == "probe":
        probe(client, args.origin, args.dest)
    else:
        start = dt.datetime.fromisoformat(args.start).replace(tzinfo=TZ) if args.start else dt.datetime.now(TZ)
        entries = client.board_range(args.kind, args.eva, start, start + dt.timedelta(hours=args.hours))
        if args.dest:
            entries = [e for e in entries if serves(e, args.dest)]
        entries.sort(key=lambda e: e.get("timeSchedule") or "")
        for e in entries:
            print(describe(e))
        print(f"\n{len(entries)} Einträge, {client.request_count} Requests")


if __name__ == "__main__":
    main()
