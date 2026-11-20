"""Client for the DB OpenBoards API (departure / arrival boards).

Spec: https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards
Free plan: 10 requests per second. Same DB-Client-Id / DB-Api-Key headers as other marketplace APIs.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import TZ

log = logging.getLogger(__name__)

BASE = "https://apis.deutschebahn.com/db-api-marketplace/apis/open-boards/v1"
TIMEOUT = 30
MAX_ENTRIES_PER_TYPE = 50
MAX_WINDOW = dt.timedelta(hours=8)
MIN_REQUEST_INTERVAL = 0.12  # stay below 10 req/s
MIN_SPLIT = dt.timedelta(minutes=10)


class OpenBoardsError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"OpenBoards HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class OpenBoardsClient:
    def __init__(self, client_id: str, api_key: str):
        if not client_id or not api_key:
            raise ValueError("DB_CLIENT_ID / DB_API_KEY fehlen")
        self.session = requests.Session()
        self.session.headers.update({
            "DB-Client-Id": client_id,
            "DB-Api-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "db-departure-monitor/2.0",
        })
        self._last = 0.0
        self.request_count = 0

    # ------------------------------------------------------------------ http
    def _post(self, path: str, body: Dict[str, Any], retries: int = 4) -> Dict[str, Any]:
        for attempt in range(retries + 1):
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.request_count += 1
            try:
                r = self.session.post(f"{BASE}{path}", json=body, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries:
                    log.warning("OpenBoards Verbindungsfehler (%s), Versuch %d", e.__class__.__name__, attempt + 1)
                    time.sleep(min(2 ** (attempt + 1), 30))
                    continue
                raise
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                time.sleep(min(2 ** (attempt + 1), 30))
                continue
            if not r.ok:
                raise OpenBoardsError(r.status_code, r.text)
            return r.json()
        raise AssertionError("unreachable")

    # ---------------------------------------------------------------- boards
    def board(self, kind: str, eva: str, start: dt.datetime, end: dt.datetime) -> Dict[str, Any]:
        """One board request. kind: 'dep' or 'arr'."""
        path = "/departures" if kind == "dep" else "/arrivals"
        body = {
            "stopPlaceOrStation": {"evaNumbers": [eva]},
            "maxEntriesPerTransportType": MAX_ENTRIES_PER_TYPE,
            "maxViaStops": 100,
            "languages": ["deu"],
            "timeWindow": {
                "timeStart": start.isoformat(timespec="seconds"),
                "timeEnd": end.isoformat(timespec="seconds"),
            },
        }
        return self._post(path, body)

    def board_range(self, kind: str, eva: str, start: dt.datetime, end: dt.datetime,
                    step: dt.timedelta = dt.timedelta(hours=2)) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetch [start, end) in windows. A window whose result was truncated is split in half.

        Returns (entries, context) where context holds the merged 'stopPlaces' and 'messages'
        of all responses (needed to resolve stada ids and cause codes).
        Note: the API filters the time window on the *best known* time, not the schedule.
        """
        step = min(step, MAX_WINDOW)
        key = "departures" if kind == "dep" else "arrivals"
        entries: Dict[str, Dict[str, Any]] = {}
        stop_places: Dict[str, Dict[str, Any]] = {}
        cause_codes: Dict[str, str] = {}  # code -> German text
        windows: List[Tuple[dt.datetime, dt.datetime]] = []
        t = start
        while t < end:
            windows.append((t, min(t + step, end)))
            t += step
        windows.reverse()
        while windows:
            a, b = windows.pop()
            data = self.board(kind, eva, a, b)
            truncated = (data.get("meta") or {}).get("truncatedTransportTypes") or []
            if truncated and (b - a) > MIN_SPLIT:
                mid = a + (b - a) / 2
                windows.extend([(mid, b), (a, mid)])
                continue
            if truncated:
                log.warning("Tafel %s %s %s-%s trotz Teilung abgeschnitten: %s", kind, eva, a, b, truncated)
            for sp in data.get("stopPlaces") or []:
                if sp.get("evaNumber"):
                    stop_places[sp["evaNumber"]] = sp
            # message ids are only unique within one response -> resolve per entry right away
            msgs = data.get("messages") or {}
            causes_here: Dict[int, Tuple[str, str]] = {}
            for m in msgs.get("risCauseCodes") or []:
                if m.get("messageID") is not None and m.get("code") is not None:
                    text = next((d.get("text") for d in m.get("descriptions") or [] if d.get("text")), "")
                    causes_here[m["messageID"]] = (str(m["code"]), text)
                    cause_codes[str(m["code"])] = text
            for e in data.get(key) or []:
                e["_causeCodes"] = sorted({causes_here[i][0] for i in e.get("messages") or [] if i in causes_here})
                uid = e.get("journeyId") or f"{e.get('departureId') or e.get('arrivalId')}"
                entries[uid] = e
        return list(entries.values()), {"stopPlaces": stop_places, "causeCodes": cause_codes}

    # --------------------------------------------------------------- lookup
    def lookup_stop_place(self, eva: str) -> Optional[Dict[str, Any]]:
        """Resolve name and stada of an EVA number via a board request (OpenBoards has no search)."""
        now = dt.datetime.now(TZ).replace(microsecond=0)
        for kind in ("dep", "arr"):
            for back in (dt.timedelta(hours=2), dt.timedelta(days=1, hours=2)):
                data = self.board(kind, eva, now - back, now - back + dt.timedelta(hours=4))
                for sp in data.get("stopPlaces") or []:
                    if sp.get("evaNumber") == eva:
                        return {"eva": eva, "name": stop_place_name(sp), "stada": sp.get("stada")}
        return None


def stop_place_name(sp: Dict[str, Any]) -> str:
    for n in sp.get("localizedNames") or sp.get("names") or []:
        if isinstance(n, dict) and n.get("name"):
            return n["name"]
    return sp.get("name") or sp.get("evaNumber") or "?"


def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
