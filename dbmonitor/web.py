"""Read-only web server: pages + aggregated JSON API.

Security model (the UI is public without login):
* the database is opened read-only (mode=ro + query_only) – nothing can be written from here
* this process needs and reads no API keys and no .env; no request triggers calls to external services
* GET only, no API docs, no CORS, strict security headers / CSP, bounded query parameters,
  generic error messages, short response cache against repeated expensive queries

Run:  python -m dbmonitor.web --db /data/train_db.db --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import logging
import os
import re
import sqlite3
import threading
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import anyio
from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import TZ, storage

log = logging.getLogger("dbmonitor.web")

STATIC = Path(__file__).resolve().parent.parent / "static"
DB_PATH = os.environ.get("DB_PATH", "./train_db.db")
PUNCTUAL_LIMIT = 6  # DB definition: less than 6 minutes late
PERIODS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "365d": 365}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

LIVE_API = re.compile(r"^/api/(health|live)$|/(live|board|trips)$")  # never cached by the browser

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """At most WEB_THREADS (8) requests are computed at the same time instead of 40: bounds memory and CPU on
    small servers."""
    anyio.to_thread.current_default_thread_limiter().total_tokens = int(os.environ.get("WEB_THREADS", "8"))
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=6)  # 9 costs much CPU for little gain


# ------------------------------------------------------------------ middleware
@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"error": "Nur lesender Zugriff"}, status_code=405, headers={"Allow": "GET, HEAD"})
    try:
        response = await call_next(request)
    except Exception:  # never leak internals
        log.exception("Fehler bei %s", request.url.path)
        response = JSONResponse({"error": "Interner Fehler"}, status_code=500)
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        live = LIVE_API.search(request.url.path) or response.status_code != 200
        h.setdefault("Cache-Control", "no-store" if live else "public, max-age=60")
    elif request.url.path.startswith("/static/vendor/"):
        h["Cache-Control"] = "public, max-age=604800"
    elif request.url.path.startswith("/static/"):
        h["Cache-Control"] = "no-cache"  # revalidate (ETag/Last-Modified -> 304), so an update shows at once
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse({"error": "Ungültige Anfrage"}, status_code=400)


@app.exception_handler(StarletteHTTPException)
async def starlette_error(_request: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": "Nicht gefunden" if exc.status_code == 404 else "Fehler"}, status_code=exc.status_code)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail if exc.status_code < 500 else "Interner Fehler"},
                        status_code=exc.status_code)


# ------------------------------------------------------------------ db + cache
# The data only changes when the collector writes (live every 10 min), so statistics may be a few minutes
# old. An expired entry is served once more while a background thread computes the new value
# (stale-while-revalidate), and concurrent requests for the same key compute it only once (single flight).
# Together this keeps a small server (the web container has 0.5 CPU) from computing the same thing twice
# and nobody waits for a recomputation after the first one.
CACHE_TTL = 300.0           # statistics
LIVE_TTL = 30.0             # boards, next trains
STALE_MAX = 3600.0          # statistics may be served this old while they are recomputed
_cache: Dict[str, Tuple[float, Any]] = {}
_jcache: "OrderedDict[str, Tuple[float, bytes, Optional[bytes]]]" = OrderedDict()   # key -> (time, json, gzip)
JCACHE_MAX_BYTES = 32 * 2**20
_cache_lock = threading.Lock()
_inflight: Dict[str, threading.Event] = {}


def _compute_once(key: str, job) -> None:
    """Run job() unless another thread already runs it for this key; then wait for that one."""
    with _cache_lock:
        ev = _inflight.get(key)
        mine = ev is None
        if mine:
            ev = _inflight[key] = threading.Event()
    if not mine:
        ev.wait(120)
        return
    try:
        job()
    finally:
        with _cache_lock:
            _inflight.pop(key, None)
        ev.set()


def _refresh_in_background(key: str, job) -> None:
    with _cache_lock:
        if key in _inflight:
            return

    def run():
        try:
            _compute_once(key, job)
        except Exception:  # keep serving the old value
            log.exception("Aktualisierung von %s fehlgeschlagen", key)
    threading.Thread(target=run, name=f"refresh:{key}", daemon=True).start()


def _store(key: str, value: Any) -> None:
    with _cache_lock:
        if len(_cache) >= 1000:  # drop the older half
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:500]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic(), value)


def cached(key: str, fn, ttl: float = CACHE_TTL, stale: float = STALE_MAX):
    with _cache_lock:
        hit = _cache.get(key)
    age = time.monotonic() - hit[0] if hit else None
    if hit and age < ttl:
        return hit[1]
    if hit and age < stale:
        _refresh_in_background(key, lambda: _store(key, fn()))
        return hit[1]
    _compute_once(key, lambda: _store(key, fn()))
    with _cache_lock:
        hit = _cache.get(key)
    return hit[1] if hit else fn()  # the other thread failed: compute (and raise) here


class PrecompressedJSON(Response):
    """JSON whose gzip form was made once when it was cached, not again for every request."""
    media_type = "application/json"

    def __init__(self, body: bytes, gz: Optional[bytes]):
        super().__init__(content=body, media_type="application/json")
        self._gz = gz

    async def __call__(self, scope, receive, send):
        accept = b",".join(v for k, v in scope.get("headers") or [] if k == b"accept-encoding")
        if self._gz is not None and b"gzip" in accept:
            self.body = self._gz
            self.headers["content-encoding"] = "gzip"   # the GZip middleware leaves it alone then
            self.headers["content-length"] = str(len(self._gz))
        self.headers["vary"] = "Accept-Encoding"
        await super().__call__(scope, receive, send)


def _store_json(key: str, value: Any) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    gz = gzip.compress(body, compresslevel=6) if len(body) > 2048 else None
    with _cache_lock:
        _jcache[key] = (time.monotonic(), body, gz)
        _jcache.move_to_end(key)
        total = sum(len(v[1]) + len(v[2] or b"") for v in _jcache.values())
        while total > JCACHE_MAX_BYTES and len(_jcache) > 1:
            _, (_, old, oldgz) = _jcache.popitem(last=False)
            total -= len(old) + len(oldgz or b"")


def cached_json(key: str, fn, ttl: float = CACHE_TTL, stale: float = STALE_MAX) -> Response:
    """For large answers (all trips of a period): keeps the encoded (and gzipped) JSON instead of Python
    objects – about a tenth of the memory – in an LRU limited to JCACHE_MAX_BYTES."""
    with _cache_lock:
        hit = _jcache.get(key)
        if hit:
            _jcache.move_to_end(key)
    age = time.monotonic() - hit[0] if hit else None
    if hit and age >= ttl and age < stale:
        _refresh_in_background(key, lambda: _store_json(key, fn()))
    elif not hit or age >= ttl:
        _compute_once(key, lambda: _store_json(key, fn()))
        with _cache_lock:
            hit = _jcache.get(key)
        if not hit:
            _store_json(key, fn())
            hit = _jcache[key]
    return PrecompressedJSON(hit[1], hit[2])


# One read-only connection per worker thread instead of one per request: no reopening, and memory-mapped
# reads go through the OS page cache. Reopened if the database file was replaced.
_local = threading.local()


def db() -> sqlite3.Connection:
    try:
        st = os.stat(DB_PATH)
    except FileNotFoundError:
        raise HTTPException(503, "Noch keine Daten")
    ident = (DB_PATH, st.st_dev, st.st_ino)
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "ident", None) != ident:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        conn = storage.connect(DB_PATH, readonly=True)
        conn.execute("PRAGMA mmap_size = 268435456")   # 256 MB address space, pages stay in the OS cache
        conn.execute("PRAGMA cache_size = -2000")      # small own cache per thread, mmap does the rest
        _local.conn, _local.ident = conn, ident
    return conn


def q_all(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in db().execute(sql, params).fetchall()]


def q_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = q_all(sql, params)
    return rows[0] if rows else None


class LocalClock:
    """Local date and time for many Unix times: one time zone conversion per hour instead of per trip."""

    def __init__(self):
        self._hours: Dict[int, Tuple[str, int, int, int]] = {}

    def __call__(self, epoch: int) -> Tuple[str, int, int, int]:
        """-> (ISO date, weekday Mon=0, hour, minute of day)"""
        b = epoch - epoch % 3600
        base = self._hours.get(b)
        if base is None:
            t = storage.local_dt(b)
            base = self._hours[b] = (t.date().isoformat(), t.weekday(), t.hour, t.minute)
        if base[3]:  # time zone with a non-whole-hour offset: no shortcut
            t = storage.local_dt(epoch)
            return t.date().isoformat(), t.weekday(), t.hour, t.hour * 60 + t.minute
        return base[0], base[1], base[2], base[2] * 60 + (epoch - b) // 60


# ------------------------------------------------------------------ helpers
FAR_FUTURE = 2**31 - 1
PERIOD_LABELS = {"1d": "Letzte 24 Stunden", "7d": "Letzte 7 Tage", "30d": "Letzte 30 Tage",
                 "90d": "Letzte 90 Tage", "365d": "Letzte 12 Monate"}
PERIOD_PATTERN = "^(1d|7d|30d|90d|365d|all)$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def period_range(period: Optional[str], date_from: Optional[str], date_to: Optional[str]) -> Tuple[int, int, str]:
    """(start, end, label) as unix seconds. Custom range wins over period."""
    today = dt.datetime.now(TZ).date()
    if date_from or date_to:
        try:
            f = dt.date.fromisoformat(date_from) if date_from else today - dt.timedelta(days=30)
            t = dt.date.fromisoformat(date_to) if date_to else today
        except ValueError:
            raise HTTPException(400, "Ungültiges Datum")
        if t < f:
            f, t = t, f
        f = max(f, dt.date(2000, 1, 1))
        return storage.day_bounds(f)[0], storage.day_bounds(t)[1], f"{f:%d.%m.%Y} – {t:%d.%m.%Y}"
    p = period or "30d"
    if p == "all":
        return 0, FAR_FUTURE, "Gesamter Zeitraum"
    days = PERIODS.get(p)
    if days is None:
        raise HTTPException(400, "Ungültiger Zeitraum")
    end = int(dt.datetime.now(TZ).timestamp())
    start = storage.day_bounds(today - dt.timedelta(days=days - 1))[0] if days > 1 else end - 86400
    return start, end, PERIOD_LABELS[p]


ISO_FIELDS = ("planned", "dep_actual", "arr_planned", "arr_actual")


def with_iso(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for r in rows:
        for f in ISO_FIELDS:
            if f in r:
                r[f] = storage.iso_local(r[f])
    return rows


# ------------------------------------------------------------------ aggregation
def _partition(conn: sqlite3.Connection, scope: str, key: str, start: int, end: int):
    """Split [start, end) into finalized full days (served from agg_*) and raw ranges (everything else).
    Returns (agg_days: set of day keys, raw_ranges: list of (a, b)) – or None if there is no data."""
    span = conn.execute("SELECT MIN(day), MAX(day) FROM agg_done WHERE scope = ? AND key = ?", (scope, key)).fetchone()
    if scope == "r":
        raw_span = conn.execute("SELECT MIN(planned), MAX(planned) FROM route_trips WHERE route_id = ?", (int(key),)).fetchone()
    else:
        raw_span = conn.execute("SELECT MIN(planned), MAX(planned) FROM stop_events WHERE board_eva = ? AND kind = ? "
                                "AND iris = 0", (key, "dep" if scope == "d" else "arr")).fetchone()
    lo = [x for x in (raw_span[0],) if x is not None]
    hi = [x for x in (raw_span[1],) if x is not None]
    if span[0] is not None:
        lo.append(storage.day_bounds(storage.key_to_date(span[0]))[0])
        hi.append(storage.day_bounds(storage.key_to_date(span[1]))[1] - 1)
    if not lo:
        return None
    start, end = max(start, min(lo)), min(end, max(hi) + 1)
    if start >= end:
        return None
    done = {r[0] for r in conn.execute("SELECT day FROM agg_done WHERE scope = ? AND key = ? AND day BETWEEN ? AND ?",
                                       (scope, key, storage.day_key(start), storage.day_key(end - 1)))}
    agg_days, raw_ranges = set(), []
    d = storage.local_dt(start).date()
    last = storage.local_dt(end - 1).date()
    while d <= last:
        d0, d1 = storage.day_bounds(d)
        k = d.year * 10000 + d.month * 100 + d.day
        if d0 >= start and d1 <= end and k in done:
            agg_days.add(k)
        else:
            a, b = max(d0, start), min(d1, end)
            if raw_ranges and raw_ranges[-1][1] == a:
                raw_ranges[-1] = (raw_ranges[-1][0], b)
            else:
                raw_ranges.append((a, b))
        d += dt.timedelta(days=1)
    return agg_days, raw_ranges


def _raw_into(result: Dict[str, Any], conn: sqlite3.Connection, scope: str, key: str, raw_ranges) -> None:
    for a, b in raw_ranges:
        part = storage.aggregate(storage.trip_rows(conn, scope, key, a, b))
        for k2, v in part["hour"].items():
            _merge_hour(result["hour"], k2, v)
        for k2, v in part["line"].items():
            cur = result["line"].setdefault(k2, {"n": 0, "m": 0, "p": 0, "f": 0, "sum_delay": 0})
            for c in cur:
                cur[c] += v[c]
        for k2, v in part["cause"].items():
            result["cause"][k2] = result["cause"].get(k2, 0) + v


def collect(conn: sqlite3.Connection, scope: str, key: str, start: int, end: int) -> Dict[str, Any]:
    """Aggregates for [start, end): finalized full days from the agg_* tables, everything else (today,
    days not finalized yet, partial days) computed from the raw rows with the same aggregate() function."""
    result: Dict[str, Any] = {"hour": {}, "line": {}, "cause": {}}
    part = _partition(conn, scope, key, start, end)
    if part is None:
        return result
    agg_days, raw_ranges = part
    if agg_days:
        lo_k, hi_k = min(agg_days), max(agg_days)
        fields = storage.AGG_HOUR_FIELDS
        for r in conn.execute(f"SELECT day, hour, {', '.join(fields)} FROM agg_hour WHERE scope = ? AND key = ? "
                              "AND day BETWEEN ? AND ?", (scope, key, lo_k, hi_k)):
            if r["day"] in agg_days:
                result["hour"][(r["day"], r["hour"])] = {f: r[f] for f in fields}
        for r in conn.execute("SELECT day, line, dir_eva, n, m, p, f, sum_delay FROM agg_line WHERE scope = ? AND key = ? "
                              "AND day BETWEEN ? AND ?", (scope, key, lo_k, hi_k)):
            if r["day"] in agg_days:
                result["line"][(r["day"], r["line"], r["dir_eva"])] = {c: r[c] for c in ("n", "m", "p", "f", "sum_delay")}
        for r in conn.execute("SELECT day, code, n FROM agg_cause WHERE scope = ? AND key = ? AND day BETWEEN ? AND ?",
                              (scope, key, lo_k, hi_k)):
            if r["day"] in agg_days:
                result["cause"][(r["day"], r["code"])] = r["n"]
    _raw_into(result, conn, scope, key, raw_ranges)
    return result


_SUM_SQL = ", ".join(f"MAX({f}) AS {f}" if f == "max_delay" else f"COALESCE(SUM({f}), 0) AS {f}"
                     for f in storage.AGG_HOUR_FIELDS)


def kpis_fast(conn: sqlite3.Connection, scope: str, key: str, start: int, end: int) -> Dict[str, Any]:
    """KPIs only: finalized days are summed inside SQLite (one query), the rest from raw rows."""
    result: Dict[str, Any] = {"hour": {}, "line": {}, "cause": {}}
    part = _partition(conn, scope, key, start, end)
    if part is None:
        return kpis_of(result)
    agg_days, raw_ranges = part
    if agg_days:
        lo_k, hi_k = min(agg_days), max(agg_days)
        # agg_days is contiguous apart from days that are not finalized – those are excluded via agg_done
        row = conn.execute(f"""SELECT {_SUM_SQL} FROM agg_hour h WHERE h.scope = ? AND h.key = ? AND h.day BETWEEN ? AND ?
                               AND EXISTS (SELECT 1 FROM agg_done d WHERE d.scope = h.scope AND d.key = h.key
                                           AND d.day = h.day)""", (scope, key, lo_k, hi_k)).fetchone()
        result["hour"][(0, 0)] = {f: row[f] for f in storage.AGG_HOUR_FIELDS}
    _raw_into(result, conn, scope, key, raw_ranges)
    return kpis_of(result)


def _merge_hour(target: Dict, key: Tuple[int, int], v: Dict[str, Any]) -> None:
    cur = target.get(key)
    if cur is None:
        target[key] = dict(v)
        return
    for f in storage.AGG_HOUR_FIELDS:
        if f == "max_delay":
            cur[f] = v[f] if cur[f] is None else (cur[f] if v[f] is None else max(cur[f], v[f]))
        else:
            cur[f] += v[f]


def _sum(rows) -> Dict[str, Any]:
    tot = {f: 0 for f in storage.AGG_HOUR_FIELDS}
    tot["max_delay"] = None
    for v in rows:
        for f in storage.AGG_HOUR_FIELDS:
            if f == "max_delay":
                if v[f] is not None:
                    tot[f] = v[f] if tot[f] is None else max(tot[f], v[f])
            else:
                tot[f] += v[f]
    return tot


def _round1(x: float) -> float:
    """Round half away from zero to one decimal (same as SQLite ROUND, not Python's banker's rounding)."""
    return float(Decimal(repr(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _pct(a, b):
    return _round1(100.0 * a / b) if b else None


def _avg(s, n):
    return _round1(s / n) if n else None


def kpis_of(agg: Dict[str, Any]) -> Dict[str, Any]:
    t = _sum(agg["hour"].values())
    failed = t["cancelled"] + t["partial"]
    return {
        "planned": t["planned"], "measured": t["measured"], "punctual": t["punctual"],
        "avg_delay": _avg(t["sum_delay"], t["measured"]), "max_delay": t["max_delay"],
        "cancelled": t["cancelled"], "partial": t["partial"], "replaced": t["replaced"], "sev": t["sev"],
        "unknown": t["unknown"], "extra": t["extra"], "platform_changed": t["platform_changed"],
        "arr_measured": t["arr_measured"], "arr_punctual": t["arr_punctual"],
        "arr_avg_delay": _avg(t["arr_sum_delay"], t["arr_measured"]),
        "punctuality": _pct(t["punctual"], t["measured"]),
        "arr_punctuality": _pct(t["arr_punctual"], t["arr_measured"]),
        "cancel_rate": _pct(failed, t["planned"]),
        "traveller_punctuality": _pct(t["punctual"], t["measured"] + failed),
    }


def analytics(conn: sqlite3.Connection, agg: Dict[str, Any], start: int, end: int) -> Dict[str, Any]:
    hours = agg["hour"]
    t = _sum(hours.values())
    dist = {k: t[k] for k in ("d0", "d1", "d6", "d16", "d31")}
    dist["failed"] = t["cancelled"] + t["partial"]
    heat_acc: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for (day, hour), v in hours.items():
        wd = int(storage.key_to_date(day).strftime("%w"))
        heat_acc.setdefault((wd, hour), []).append(v)
    heat = []
    for (wd, h), vs in heat_acc.items():
        s = _sum(vs)
        heat.append({"wd": wd, "h": h, "n": s["planned"], "m": s["measured"], "p": s["punctual"],
                     "f": s["cancelled"] + s["partial"], "avg": _avg(s["sum_delay"], s["measured"])})
    days = sorted({day for day, _ in hours})
    span_days = (dt.datetime.fromtimestamp(min(end, FAR_FUTURE), TZ) - dt.datetime.fromtimestamp(start, TZ)).days \
        if start > 0 else 9999
    weekly = span_days > 120
    buckets: Dict[str, Dict[str, Any]] = {}
    for (day, hour), v in hours.items():
        date = storage.key_to_date(day)
        b = date.strftime("%Y-W%W") if weekly else date.isoformat()
        e = buckets.setdefault(b, {"vals": [], "first": date})
        e["vals"].append(v)
        e["first"] = min(e["first"], date)
    trend = []
    for b in sorted(buckets):
        s = _sum(buckets[b]["vals"])
        trend.append({"b": b, "first_day": buckets[b]["first"].isoformat(), "m": s["measured"], "p": s["punctual"],
                      "f": s["cancelled"] + s["partial"], "n": s["planned"], "avg": _avg(s["sum_delay"], s["measured"])})
    cause_tot: Dict[str, int] = {}
    for (_, code), n in agg["cause"].items():
        cause_tot[code] = cause_tot.get(code, 0) + n
    texts = {r[0]: r[1] for r in conn.execute("SELECT code, text FROM cause_codes")}
    causes = [{"code": c, "text": texts.get(c) or f"Sonstiger Grund (Code {c})", "n": n}
              for c, n in sorted(cause_tot.items(), key=lambda x: (-x[1], x[0]))[:8]]
    return {"distribution": dist, "heatmap": heat, "trend": trend, "trend_bucket": "week" if weekly else "day",
            "causes": causes, "days": len(days)}


def lines_of(conn: sqlite3.Connection, agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    tot: Dict[Tuple[str, str], Dict[str, int]] = {}
    for (_, line, dir_eva), v in agg["line"].items():
        cur = tot.setdefault((line, dir_eva), {"n": 0, "m": 0, "p": 0, "f": 0, "sum_delay": 0})
        for c in cur:
            cur[c] += v[c]
    names = {r[0]: r[1] for r in conn.execute("SELECT eva, name FROM stations")}
    rows = [{"line": line, "dir_eva": d or None, "direction": names.get(d), "n": v["n"], "m": v["m"], "p": v["p"],
             "f": v["f"], "avg": _avg(v["sum_delay"], v["m"])} for (line, d), v in tot.items() if v["n"] >= 5]
    rows.sort(key=lambda r: (-r["n"], r["line"], r["dir_eva"] or ""))
    return rows[:16]


# problem trains straight from the raw rows (coupled trains count once)
ROUTE_TRIPS_CTE = """
WITH trips AS (
  SELECT t.* FROM route_trips t
  WHERE t.route_id = ? AND t.planned >= ? AND t.planned < ?
    AND (t.coupled_jid IS NULL OR t.jid = t.coupled_jid OR NOT EXISTS (
         SELECT 1 FROM route_trips t2 WHERE t2.route_id = t.route_id AND t2.jid = t.coupled_jid
                AND t2.planned BETWEEN t.planned - 86400 AND t.planned + 86400))
)
"""
STATION_TRIPS_CTE = """
WITH trips AS (
  SELECT e.planned, e.jid, e.category, e.number, e.line, e.st_status AS status,
         CASE WHEN e.st_real = 1 THEN 'REAL' ELSE e.time_type END AS time_type, e.st_delay AS delay_min
  FROM stop_events e
  WHERE e.board_eva = ? AND e.kind = ? AND e.iris = 0 AND e.planned >= ? AND e.planned < ?
    AND e.st_status IS NOT NULL AND e.st_status <> 'ignore'
    AND (e.coupled_jid IS NULL OR e.jid = e.coupled_jid OR NOT EXISTS (
         SELECT 1 FROM stop_events e2 WHERE e2.board_eva = e.board_eva AND e2.kind = e.kind
                AND e2.jid = e.coupled_jid))
)
"""


def problem_trains(conn: sqlite3.Connection, cte: str, args: tuple) -> List[Dict[str, Any]]:
    ok_real = "status IN ('ok','replaced') AND time_type='REAL'"
    rows = conn.execute(cte + f"""
        SELECT category, number, MAX(line) line, COUNT(*) n,
               COUNT(*) FILTER (WHERE {ok_real}) m,
               COUNT(*) FILTER (WHERE {ok_real} AND delay_min < {PUNCTUAL_LIMIT}) p,
               COUNT(*) FILTER (WHERE status IN ('cancelled','partial')) f,
               ROUND(AVG(delay_min) FILTER (WHERE {ok_real}), 1) avg,
               MIN(planned) first_planned
        FROM trips WHERE status <> 'replacement' GROUP BY category, number HAVING n >= 5
        ORDER BY (1.0 * (m - p + f) / n) DESC, avg DESC, category, number LIMIT 10""", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # usual departure time = earliest local clock time of this train in the period
        times = [storage.local_dt(p[0]).strftime("%H:%M") for p in conn.execute(
            cte + "SELECT planned FROM trips WHERE category IS ? AND number IS ? AND status <> 'replacement'",
            args + (r["category"], r["number"]))]
        d["usual_time"] = min(times) if times else None
        d.pop("first_planned", None)
        out.append(d)
    return out


# ------------------------------------------------------------------ shared lookups
def route_row(conn: sqlite3.Connection, route_id: int) -> Dict[str, Any]:
    r = conn.execute("""
        SELECT r.id, r.origin_eva, r.dest_eva, so.name origin_name, sd.name dest_name,
               so.lat origin_lat, so.lon origin_lon, sd.lat dest_lat, sd.lon dest_lon,
               (SELECT id FROM routes x WHERE x.origin_eva = r.dest_eva AND x.dest_eva = r.origin_eva AND x.active = 1) reverse_id
        FROM routes r LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
        WHERE r.id = ? AND r.active = 1""", (route_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Strecke nicht gefunden")
    return dict(r)


def route_kpis(conn: sqlite3.Connection, route_id: int, start: int, end: int) -> Dict[str, Any]:
    return kpis_fast(conn, "r", str(route_id), start, end)


LIVE_COLS = ("planned, category, number, line, status, time_type, dep_actual, delay_min, arr_planned, arr_actual, "
             "arr_delay_min, planned_platform, platform, platform_changed, extra, replacement_label")
EFFECTIVE = "(CASE WHEN status IN ('cancelled','partial') THEN planned ELSE COALESCE(dep_actual, planned) END)"


def live_rows(conn: sqlite3.Connection, route_id: int, limit_past: int = 4, limit_next: int = 8) -> Dict[str, List]:
    now = int(dt.datetime.now(TZ).timestamp())
    nxt = [dict(r) for r in conn.execute(
        f"""SELECT {LIVE_COLS} FROM route_trips WHERE route_id = ? AND status <> 'replacement'
            AND planned >= ? AND {EFFECTIVE} >= ? AND planned < ? ORDER BY planned LIMIT ?""",
        (route_id, now - 86400, now, now + 6 * 3600, limit_next))]
    past = [dict(r) for r in conn.execute(
        f"""SELECT {LIVE_COLS} FROM route_trips WHERE route_id = ? AND status <> 'replacement'
            AND planned >= ? AND {EFFECTIVE} < ? ORDER BY planned DESC LIMIT ?""",
        (route_id, now - 6 * 3600, now, limit_past))] if limit_past else []
    return {"past": with_iso(list(reversed(past))), "next": with_iso(nxt)}


def route_path(conn: sqlite3.Connection, route_id: int, stadas=None) -> List[Dict[str, Any]]:
    """Typical stop sequence with coordinates (stops without coordinates are skipped)."""
    evas = storage.route_geometry(conn, route_id, stadas=stadas)
    if not evas:
        return []
    rows = {r["eva"]: r for r in conn.execute(
        f"SELECT eva, name, lat, lon FROM stations WHERE eva IN ({','.join('?' * len(evas))})", evas)}
    return [{"eva": e, "name": rows[e]["name"], "lat": rows[e]["lat"], "lon": rows[e]["lon"]}
            for e in evas if e in rows and rows[e]["lat"] is not None]


def spark(conn: sqlite3.Connection, scope: str, key: str) -> List[Dict[str, Any]]:
    """Daily punctuality of the last 30 days (finalized days summed in SQLite, the rest from raw rows)."""
    start = storage.day_bounds(dt.datetime.now(TZ).date() - dt.timedelta(days=29))[0]
    per_day: Dict[int, List[int]] = {}
    part = _partition(conn, scope, key, start, FAR_FUTURE)
    if part is None:
        return []
    agg_days, raw_ranges = part
    if agg_days:
        for r in conn.execute("""SELECT day, SUM(punctual), SUM(measured) FROM agg_hour h WHERE scope = ? AND key = ?
                                 AND day BETWEEN ? AND ? AND EXISTS (SELECT 1 FROM agg_done d WHERE d.scope = h.scope
                                 AND d.key = h.key AND d.day = h.day) GROUP BY day""",
                              (scope, key, min(agg_days), max(agg_days))):
            per_day[r[0]] = [r[1], r[2]]
    raw: Dict[str, Any] = {"hour": {}, "line": {}, "cause": {}}
    _raw_into(raw, conn, scope, key, raw_ranges)
    for (day, _), v in raw["hour"].items():
        cur = per_day.setdefault(day, [0, 0])
        cur[0] += v["punctual"]
        cur[1] += v["measured"]
    return [{"day": storage.key_to_date(d).isoformat(), "p": _pct(p, m)} for d, (p, m) in sorted(per_day.items())]


def with_conn(fn):
    return fn(db())


# ------------------------------------------------------------------ API: routes
@app.get("/api/health")
def health():
    def build(conn):
        r = conn.execute("SELECT value FROM collector_state WHERE key = 'last_data_update'").fetchone()
        return {"status": "ok", "last_update": r[0] if r else None}
    return with_conn(build)


@app.get("/api/overview")
def overview(period: str = Query("7d", pattern=PERIOD_PATTERN)):
    def build(conn):
        start, end, label = period_range(period, None, None)
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, r.origin_eva, r.dest_eva, so.name origin_name, sd.name dest_name,
                   so.lat origin_lat, so.lon origin_lon, sd.lat dest_lat, sd.lon dest_lon
            FROM routes r LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 ORDER BY so.name, sd.name""")]
        def all_paths(c):  # changes only when the stop pattern of a route changes
            stadas = storage.stada_map(c)
            return {r["id"]: route_path(c, r["id"], stadas) for r in routes}
        paths = cached("paths:" + ",".join(str(r["id"]) for r in routes), lambda: with_conn(all_paths), ttl=6 * 3600)
        for r in routes:
            r["kpis"] = route_kpis(conn, r["id"], start, end)
            hs = hourly_stats(_route_rows(conn, r["id"], start, end))
            r["dist"], r["worst"] = hs["all"], hs["worst"]
            r["spark"] = spark(conn, "r", str(r["id"]))
            r["next"] = live_rows(conn, r["id"], 0, 3)["next"]
            r["path"] = paths.get(r["id"]) or route_path(conn, r["id"])
        last = conn.execute("SELECT value FROM collector_state WHERE key = 'last_data_update'").fetchone()
        return {"period": period, "label": label, "routes": routes, "last_update": last[0] if last else None}
    return cached(f"overview:{period}", lambda: with_conn(build), ttl=120, stale=600)  # includes next trains


@app.get("/api/routes/{route_id}")
def route_detail(route_id: int = PathParam(..., ge=1, le=10**9),
                 period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
                 date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                 date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN)):
    def build(conn):
        r = route_row(conn, route_id)
        start, end, label = period_range(period, date_from, date_to)
        agg = collect(conn, "r", str(route_id), start, end)
        reverse = route_kpis(conn, r["reverse_id"], start, end) if r["reverse_id"] else None
        return {"route": r, "period": period, "label": label, "kpis": kpis_of(agg), "reverse_kpis": reverse,
                **analytics(conn, agg, start, end),
                "trains": problem_trains(conn, ROUTE_TRIPS_CTE, (route_id, start, end))}
    return cached(f"route:{route_id}:{period}:{date_from}:{date_to}", lambda: with_conn(build))


@app.get("/api/routes/{route_id}/live")
def route_live(route_id: int = PathParam(..., ge=1, le=10**9)):
    def build(conn):
        route_row(conn, route_id)
        return live_rows(conn, route_id)
    return cached(f"live:{route_id}", lambda: with_conn(build), ttl=LIVE_TTL, stale=2 * LIVE_TTL)


STATUS_FILTERS = {
    "late": f"status IN ('ok','replaced') AND time_type='REAL' AND delay_min >= {PUNCTUAL_LIMIT}",
    "failed": "status IN ('cancelled','partial')",
    "sev": "status = 'sev'",
    "replaced": "status = 'replaced'",
    "unknown": "status = 'unknown'",
}


@app.get("/api/routes/{route_id}/trips")
def route_trips(route_id: int = PathParam(..., ge=1, le=10**9),
                period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
                date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN),
                status: Optional[str] = Query(None, pattern="^(late|failed|sev|replaced|unknown)$"),
                q: Optional[str] = Query(None, max_length=30, pattern=r"^[\w .:-]*$"),
                limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000)):
    def build(conn):
        route_row(conn, route_id)
        start, end, _ = period_range(period, date_from, date_to)
        where, params = ["status <> 'replacement'"], [route_id, start, end]
        if status:
            where.append(STATUS_FILTERS[status])
        if q:
            where.append("(IFNULL(category,'') || ' ' || IFNULL(number,'') LIKE ? OR IFNULL(line,'') LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        w = " AND ".join(where)
        total = conn.execute(ROUTE_TRIPS_CTE + f"SELECT COUNT(*) FROM trips WHERE {w}", tuple(params)).fetchone()[0]
        rows = [dict(r) for r in conn.execute(ROUTE_TRIPS_CTE + f"""
            SELECT planned, category, number, line, status, time_type, dep_actual, delay_min,
                   arr_planned, arr_actual, arr_delay_min, planned_platform, platform, platform_changed, extra,
                   replacement_label, cause_codes, CASE WHEN iris = 1 THEN 'iris' ELSE 'openboards' END AS source
            FROM trips WHERE {w} ORDER BY planned DESC LIMIT ? OFFSET ?""", tuple(params + [limit, offset]))]
        return {"total": total, "trips": with_iso(rows)}
    return with_conn(build)


SAMPLE_STATUS = ("ok", "cancelled", "partial", "sev", "replaced", "unknown")
FAILED = ("cancelled", "partial")


def _route_rows(conn: sqlite3.Connection, route_id: int, start: int, end: int) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT planned, jid, coupled_jid, category, number, line, status, time_type, delay_min, arr_delay_min,
               cause_codes, NULL AS direction
        FROM route_trips WHERE route_id = ? AND planned >= ? AND planned < ? AND status <> 'replacement'
        ORDER BY planned""", (route_id, start, end)).fetchall()


def _station_rows(conn: sqlite3.Connection, eva: str, kind: str, start: int, end: int) -> List[sqlite3.Row]:
    """Station view (all trains at the board), shaped like _route_rows(); direction = destination / origin."""
    return conn.execute("""
        SELECT e.planned, e.jid, e.coupled_jid, e.category, e.number, e.line, e.st_status AS status,
               CASE WHEN e.st_real = 1 THEN 'REAL' ELSE e.time_type END AS time_type, e.st_delay AS delay_min,
               NULL AS arr_delay_min, e.cause_codes, s.name AS direction
        FROM stop_events e
        LEFT JOIN stations s ON s.eva = CASE WHEN e.kind = 'dep' THEN e.dest_eva ELSE e.origin_eva END
        WHERE e.board_eva = ? AND e.kind = ? AND e.iris = 0 AND e.planned >= ? AND e.planned < ?
          AND e.st_status IS NOT NULL AND e.st_status NOT IN ('ignore', 'replacement')
        ORDER BY e.planned""", (eva, kind, start, end)).fetchall()


def _dedupe(rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
    """Coupled trains (one physical train, several journeys) count once – same rule as storage.aggregate()."""
    jids = {r["jid"] for r in rows}
    return [r for r in rows if r["coupled_jid"] is None or r["coupled_jid"] == r["jid"] or r["coupled_jid"] not in jids]


def _measured(r: sqlite3.Row) -> Optional[int]:
    ok = r["status"] in storage.COUNTED and r["time_type"] == "REAL" and r["delay_min"] is not None
    return r["delay_min"] if ok else None


def samples_of(conn: sqlite3.Connection, rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """Every trip in a compact form, so the browser can compute quantiles for any selection (hour, weekday,
    single cell, day, train). Same counting rules as the aggregates: replacement trips are left out,
    coupled trains count once, a delay is only given when it is measured.

    trips:  [day index, minute of day, status index, delay | null, arrival delay | null, train index,
             cause set index (0 = none)]
    trains: [category, number, line, direction]"""
    days: Dict[str, int] = {}
    trains: Dict[Tuple[str, str, str, str], int] = {}
    cause_sets: Dict[Tuple[str, ...], int] = {(): 0}
    out: List[List[Any]] = []
    clock = LocalClock()
    for r in _dedupe(rows):
        day, _, _, minute = clock(r["planned"])
        di = days.setdefault(day, len(days))
        ti = trains.setdefault((r["category"] or "", r["number"] or "", r["line"] or "", r["direction"] or ""),
                               len(trains))
        st = r["status"]
        codes: Tuple[str, ...] = ()
        if st in FAILED or (r["delay_min"] or 0) >= PUNCTUAL_LIMIT:
            codes = tuple(c for c in storage.decode_causes(r["cause_codes"]) if c != "00")
        ci = cause_sets.setdefault(codes, len(cause_sets))
        out.append([di, minute, SAMPLE_STATUS.index(st) if st in SAMPLE_STATUS else 5,
                    _measured(r), r["arr_delay_min"], ti, ci])
    used = {c for s in cause_sets for c in s}
    texts = {r[0]: r[1] for r in conn.execute("SELECT code, text FROM cause_codes")} if used else {}
    return {"days": list(days), "statuses": list(SAMPLE_STATUS),
            "trains": [list(k) for k in trains], "cause_sets": [list(k) for k in cause_sets],
            "cause_text": {c: texts.get(c) or f"Sonstiger Grund (Code {c})" for c in sorted(used)},
            "trips": out}


def route_samples(conn: sqlite3.Connection, route_id: int, start: int, end: int) -> Dict[str, Any]:
    return samples_of(conn, _route_rows(conn, route_id, start, end))


def _quant(s: List[int], p: float) -> Optional[float]:
    """Linear interpolation between closest ranks (same as the browser code)."""
    if not s:
        return None
    i = (len(s) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return _round1(s[lo] + (s[hi] - s[lo]) * (i - lo))


def delay_stats(delays: List[int], n: int, failed: int) -> Dict[str, Any]:
    s = sorted(delays)
    ok = sum(1 for d in s if d < PUNCTUAL_LIMIT)
    return {"n": n, "m": len(s), "p": ok, "f": failed, "punctuality": _pct(ok, len(s)),
            "p10": _quant(s, .1), "p25": _quant(s, .25), "p50": _quant(s, .5), "p75": _quant(s, .75),
            "p90": _quant(s, .9)}


def hourly_stats(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """Quantiles overall and per departure hour, plus the worst weekday/hour cell (min. 8 measured)."""
    acc: Dict[Any, List[Any]] = {}
    clock = LocalClock()
    for r in _dedupe(rows):
        _, wd, hour, _ = clock(r["planned"])
        d = _measured(r)
        for k in ("all", hour, (wd, hour)):
            a = acc.setdefault(k, [[], 0, 0])
            a[1] += 1
            if d is not None:
                a[0].append(d)
            if r["status"] in FAILED:
                a[2] += 1
    hours = [{"h": h, **delay_stats(*acc[h])} for h in range(24) if h in acc]
    worst = None
    for k, (ds, _, _) in acc.items():
        if isinstance(k, tuple) and len(ds) >= 8:
            p = _pct(sum(1 for d in ds if d < PUNCTUAL_LIMIT), len(ds))
            if worst is None or p < worst["punctuality"]:
                worst = {"wd": k[0], "h": k[1], "punctuality": p}
    return {"all": delay_stats(*acc.get("all", [[], 0, 0])), "hours": hours, "worst": worst}


def _prev(start: int, end: int, fn) -> Optional[Dict[str, Any]]:
    """KPIs of the same length directly before, for the arrows in the summary."""
    return fn(start - (end - start), start) if start > 0 and end < FAR_FUTURE else None


@app.get("/api/routes/{route_id}/samples")
def route_samples_api(route_id: int = PathParam(..., ge=1, le=10**9),
                      period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
                      date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                      date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN)):
    def build(conn):
        r = route_row(conn, route_id)
        start, end, label = period_range(period, date_from, date_to)
        reverse = route_kpis(conn, r["reverse_id"], start, end) if r["reverse_id"] else None
        return {"route": r, "label": label, "kpis": route_kpis(conn, route_id, start, end),
                "prev_kpis": _prev(start, end, lambda a, b: route_kpis(conn, route_id, a, b)),
                "reverse_kpis": reverse, **route_samples(conn, route_id, start, end)}
    return cached_json(f"samples:{route_id}:{period}:{date_from}:{date_to}", lambda: with_conn(build))


# ------------------------------------------------------------------ API: stations
OBSERVED_SQL = """
    SELECT origin_eva AS eva FROM routes WHERE active = 1 UNION SELECT dest_eva FROM routes WHERE active = 1
    UNION SELECT eva FROM watched_stations WHERE active = 1
"""


def station_kpis(conn: sqlite3.Connection, eva: str, kind: str, start: int, end: int) -> Dict[str, Any]:
    return kpis_fast(conn, "d" if kind == "dep" else "a", eva, start, end)


def observed_stations(conn: sqlite3.Connection, start: int, end: int, with_spark: bool = False) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in conn.execute(f"""
        SELECT s.eva, s.name, s.lat, s.lon,
               (SELECT COUNT(*) FROM routes r WHERE r.active = 1 AND (r.origin_eva = s.eva OR r.dest_eva = s.eva)) routes
        FROM stations s WHERE s.eva IN ({OBSERVED_SQL}) ORDER BY s.name""")]
    first = conn.execute("SELECT MIN(planned) FROM stop_events WHERE iris = 0 AND st_status IS NOT NULL").fetchone()[0]
    data_start = max(start, first or start)
    days = max(1.0, (min(end, int(time.time())) - data_start) / 86400) if data_start else 1.0
    for r in rows:
        r["dep"] = station_kpis(conn, r["eva"], "dep", start, end)
        r["arr"] = station_kpis(conn, r["eva"], "arr", start, end)
        r["per_day"] = round(r["dep"]["planned"] / days) if r["dep"]["planned"] else 0
        if with_spark:
            r["spark"] = spark(conn, "d", r["eva"])
    return rows


@app.get("/api/stations")
def stations(period: str = Query("7d", pattern=PERIOD_PATTERN)):
    def build(conn):
        start, end, label = period_range(period, None, None)
        since = conn.execute("SELECT MIN(planned) FROM stop_events WHERE iris = 0").fetchone()[0]
        return {"label": label, "stations": observed_stations(conn, start, end, with_spark=True),
                "since": storage.iso_local(since)}
    return cached(f"stations:{period}", lambda: with_conn(build))


def station_board(conn: sqlite3.Connection, eva: str, kind: str, limit: int = 14) -> List[Dict[str, Any]]:
    now = int(dt.datetime.now(TZ).timestamp())
    rows = [dict(r) for r in conn.execute("""
        SELECT e.planned, e.category, e.number, e.line, e.st_status AS status, e.time_type,
               e.actual AS dep_actual,
               CASE WHEN e.st_status = 'replaced' THEN e.st_delay ELSE e.delay_min END AS delay_min,
               e.platform, e.planned_platform,
               CASE WHEN e.platform IS NOT NULL AND e.planned_platform IS NOT NULL
                         AND e.platform <> e.planned_platform THEN 1 ELSE 0 END AS platform_changed,
               e.replaced_by_label AS replacement_label, s.name AS direction
        FROM stop_events e
        LEFT JOIN stations s ON s.eva = CASE WHEN e.kind = 'dep' THEN e.dest_eva ELSE e.origin_eva END
        WHERE e.board_eva = ? AND e.kind = ? AND e.iris = 0 AND e.planned >= ? AND e.planned < ?
          AND COALESCE(e.st_status, 'ok') NOT IN ('ignore', 'replacement')
          AND (CASE WHEN e.st_status = 'cancelled' THEN e.planned ELSE COALESCE(e.actual, e.planned) END) >= ?
        ORDER BY COALESCE(e.actual, e.planned) LIMIT ?""",
        (eva, kind, now - 86400, now + 4 * 3600, now - 180, limit))]
    return with_iso(rows)


def observed(conn: sqlite3.Connection, eva: str) -> Dict[str, Any]:
    s = conn.execute(f"SELECT eva, name, lat, lon FROM stations WHERE eva = ? AND eva IN ({OBSERVED_SQL})", (eva,)).fetchone()
    if not s:
        raise HTTPException(404, "Bahnhof wird nicht beobachtet")
    return dict(s)


@app.get("/api/stations/{eva}")
def station(eva: str = PathParam(..., pattern=r"^\d{6,9}$"),
            period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
            date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
            date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN),
            kind: str = Query("dep", pattern="^(dep|arr)$")):
    def build(conn):
        s = observed(conn, eva)
        start, end, label = period_range(period, date_from, date_to)
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, r.origin_eva, r.dest_eva, so.name origin_name, sd.name dest_name
            FROM routes r LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 AND (r.origin_eva = ? OR r.dest_eva = ?) ORDER BY r.origin_eva <> ?, sd.name, so.name""",
            (eva, eva, eva))]
        for r in routes:
            r["kpis"] = route_kpis(conn, r["id"], start, end)
        aggs = {k: collect(conn, "d" if k == "dep" else "a", eva, start, end) for k in ("dep", "arr")}
        agg = aggs[kind]
        return {"station": s, "label": label, "kind": kind, "routes": routes, "watched_only": not routes,
                "dep": kpis_of(aggs["dep"]), "arr": kpis_of(aggs["arr"]), "lines": lines_of(conn, agg),
                **analytics(conn, agg, start, end),
                "trains": problem_trains(conn, STATION_TRIPS_CTE, (eva, kind, start, end))}
    return cached(f"station:{eva}:{period}:{date_from}:{date_to}:{kind}", lambda: with_conn(build))


@app.get("/api/stations/{eva}/samples")
def station_samples(eva: str = PathParam(..., pattern=r"^\d{6,9}$"),
                    period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
                    date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                    date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN),
                    kind: str = Query("dep", pattern="^(dep|arr)$")):
    """Station cockpit: like /api/routes/{id}/samples, for all trains at the station (departures or arrivals)."""
    def build(conn):
        st = observed(conn, eva)
        start, end, label = period_range(period, date_from, date_to)
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, r.origin_eva, r.dest_eva, so.name origin_name, sd.name dest_name
            FROM routes r LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 AND (r.origin_eva = ? OR r.dest_eva = ?) ORDER BY r.origin_eva <> ?, sd.name, so.name""",
            (eva, eva, eva))]
        for r in routes:
            r["kpis"] = route_kpis(conn, r["id"], start, end)
        first = conn.execute("SELECT MIN(planned) FROM stop_events WHERE iris = 0 AND st_status IS NOT NULL").fetchone()[0]
        return {"station": st, "label": label, "kind": kind, "routes": routes, "watched_only": not routes,
                "kpis": station_kpis(conn, eva, kind, start, end),
                "other_kpis": station_kpis(conn, eva, "arr" if kind == "dep" else "dep", start, end),
                "prev_kpis": _prev(start, end, lambda a, b: station_kpis(conn, eva, kind, a, b)),
                "since": storage.iso_local(first), **samples_of(conn, _station_rows(conn, eva, kind, start, end))}
    return cached_json(f"ssamples:{eva}:{period}:{date_from}:{date_to}:{kind}", lambda: with_conn(build))


def live_board(conn: sqlite3.Connection, eva: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Departure board for /live: like station_board() plus the next stops ("über …") and whether the train
    serves one of the routes starting here."""
    now = int(dt.datetime.now(TZ).timestamp())
    dests = {r[0] for r in conn.execute("SELECT dest_eva FROM routes WHERE active = 1 AND origin_eva = ?", (eva,))}
    rows = [dict(r) for r in conn.execute("""
        SELECT e.planned, e.category, e.number, e.line, e.st_status AS status, e.time_type, e.actual AS dep_actual,
               CASE WHEN e.st_status = 'replaced' THEN e.st_delay ELSE e.delay_min END AS delay_min,
               e.platform, e.planned_platform,
               CASE WHEN e.platform IS NOT NULL AND e.planned_platform IS NOT NULL
                         AND e.platform <> e.planned_platform THEN 1 ELSE 0 END AS platform_changed,
               e.replaced_by_label AS replacement_label, e.dest_eva, s.name AS direction, e.path
        FROM stop_events e LEFT JOIN stations s ON s.eva = e.dest_eva
        WHERE e.board_eva = ? AND e.kind = 'dep' AND e.iris = 0 AND e.planned >= ? AND e.planned < ?
          AND COALESCE(e.st_status, 'ok') NOT IN ('ignore', 'replacement')
          AND (CASE WHEN e.st_status = 'cancelled' THEN e.planned ELSE COALESCE(e.actual, e.planned) END) >= ?
        ORDER BY e.planned LIMIT ?""", (eva, now - 86400, now + 6 * 3600, now - 60, limit))]
    evas = {s["e"] for r in rows for s in (storage.decode_path(r["path"], {}) or []) if s["e"]}
    names = {}
    if evas:
        names = {r[0]: r[1] for r in conn.execute(
            f"SELECT eva, name FROM stations WHERE eva IN ({','.join('?' * len(evas))})", tuple(evas))}
    for r in rows:
        path = storage.decode_path(r.pop("path"), {}) or []
        stops = [s for s in path if s["e"] and s["e"] != r["dest_eva"] and not s.get("x")]
        r["via"] = [names[s["e"]] for s in stops if s["e"] in names][:5]
        r["mine"] = bool(dests & ({s["e"] for s in path} | {r["dest_eva"]}))
        r.pop("dest_eva")
    return with_iso(rows)


@app.get("/api/live")
def live(eva: Optional[str] = Query(None, pattern=r"^\d{6,9}$")):
    """Everything the /live page needs in one request: the stations with routes, the board of one of them
    and the next trains of the routes starting there."""
    def build(conn):
        stations = [dict(r) for r in conn.execute("""
            SELECT s.eva, s.name, COUNT(*) routes FROM routes r JOIN stations s ON s.eva = r.origin_eva
            WHERE r.active = 1 GROUP BY s.eva ORDER BY s.name""")]
        if not stations:
            return {"stations": [], "eva": None, "board": [], "routes": [], "last_update": None}
        # default: the station where most routes start (usually home)
        sel = eva if eva in {s["eva"] for s in stations} else max(stations, key=lambda s: s["routes"])["eva"]
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, so.name origin_name, sd.name dest_name FROM routes r
            LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 AND r.origin_eva = ? ORDER BY sd.name""", (sel,))]
        week = period_range("7d", None, None)
        for r in routes:
            r["next"] = live_rows(conn, r["id"], 0, 3)["next"]
            r["punctuality_7d"] = route_kpis(conn, r["id"], week[0], week[1])["punctuality"]
        last = conn.execute("SELECT value FROM collector_state WHERE key = 'last_data_update'").fetchone()
        return {"stations": stations, "eva": sel, "board": live_board(conn, sel), "routes": routes,
                "last_update": last[0] if last else None}
    return cached(f"livepage:{eva}", lambda: with_conn(build), ttl=LIVE_TTL, stale=2 * LIVE_TTL)


@app.get("/api/stations/{eva}/board")
def station_live(eva: str = PathParam(..., pattern=r"^\d{6,9}$")):
    def build(conn):
        observed(conn, eva)
        return {"dep": station_board(conn, eva, "dep"), "arr": station_board(conn, eva, "arr")}
    return cached(f"board:{eva}", lambda: with_conn(build), ttl=LIVE_TTL, stale=2 * LIVE_TTL)


@app.get("/api/compare")
def compare(kind: str = Query("routes", pattern="^(routes|stations)$"),
            period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
            date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
            date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN)):
    def build(conn):
        start, end, label = period_range(period, date_from, date_to)
        if kind == "stations":
            return {"label": label, "stations": observed_stations(conn, start, end)}
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, so.name origin_name, sd.name dest_name, r.origin_eva, r.dest_eva
            FROM routes r LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1""")]
        for r in routes:
            r["kpis"] = route_kpis(conn, r["id"], start, end)
        return {"label": label, "routes": routes}
    return cached(f"compare:{kind}:{period}:{date_from}:{date_to}", lambda: with_conn(build))


@app.get("/api/catalog")
def catalog():
    """Names only: all routes and observed stations (for pickers)."""
    def build(conn):
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, so.name origin_name, sd.name dest_name FROM routes r
            LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 ORDER BY so.name, sd.name""")]
        stations = [dict(r) for r in conn.execute(f"SELECT eva, name FROM stations WHERE eva IN ({OBSERVED_SQL}) ORDER BY name")]
        return {"routes": routes, "stations": stations}
    return cached("catalog", lambda: with_conn(build), ttl=300)


@app.get("/api/compare/hourly")
def compare_hourly(period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
                   date_from: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
                   date_to: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN)):
    """Matrix route x departure hour: share punctual and quantiles per hour, for the comparison page."""
    def build(conn):
        start, end, label = period_range(period, date_from, date_to)
        routes = [dict(r) for r in conn.execute("""
            SELECT r.id, so.name origin_name, sd.name dest_name FROM routes r
            LEFT JOIN stations so ON so.eva = r.origin_eva LEFT JOIN stations sd ON sd.eva = r.dest_eva
            WHERE r.active = 1 ORDER BY so.name, sd.name""")]
        for r in routes:
            r["kpis"] = route_kpis(conn, r["id"], start, end)
            r.update(hourly_stats(_route_rows(conn, r["id"], start, end)))
        return {"label": label, "routes": routes}
    return cached_json(f"hourly:{period}:{date_from}:{date_to}", lambda: with_conn(build))


# ------------------------------------------------------------------ pages
def page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def index_page():
    return page("index.html")


@app.get("/route/{route_id}", include_in_schema=False)
def route_page(route_id: int = PathParam(..., ge=1, le=10**9)):
    return page("route.html")


@app.get("/station/{eva}", include_in_schema=False)
def station_page(eva: str = PathParam(..., pattern=r"^\d{6,9}$")):
    return page("station.html")


@app.get("/stations", include_in_schema=False)
def stations_page():
    return page("stations.html")


@app.get("/stats", include_in_schema=False)
def stats_page():
    return page("stats.html")


@app.get("/analyse", include_in_schema=False)
def analyse_page():
    return page("analyse.html")


@app.get("/live", include_in_schema=False)
def live_page():
    return page("live.html")


@app.get("/datenschutz", include_in_schema=False)
def privacy_page():
    return page("privacy.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "img" / "favicon.svg", media_type="image/svg+xml")


def main() -> None:
    global DB_PATH
    ap = argparse.ArgumentParser(description="DB Departure Monitor – Weboberfläche (nur lesend)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--host", default=os.environ.get("WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    args = ap.parse_args()
    DB_PATH = args.db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, server_header=False, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
