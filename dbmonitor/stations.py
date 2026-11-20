"""Station lookup without any additional API subscription.

* Search: the local `stations` table. Every OpenBoards response lists all stops of the trains at the
  observed stations (name, EVA number, stada), so after the first collector run it already knows the
  surrounding network. `dbmon search` / `dbmon add` search there.
* Unknown station: enter the EVA number directly; it is verified with an OpenBoards request
  (name + stada come back with the board).
* Coordinates for the map: OpenStreetMap Nominatim (free, no key, max. 1 request/s), only from the
  collector and only for configured stations. Can be switched off with GEOCODING=off.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "db-departure-monitor/3.0"


def search_local(conn: sqlite3.Connection, text: str, limit: int = 20) -> List[sqlite3.Row]:
    """Stations already known from OpenBoards responses; railway stops (with stada) first."""
    words = [w for w in text.replace("(", " ").replace(")", " ").split() if w]
    if not words:
        return []
    where = " AND ".join("lower(name) LIKE ?" for _ in words)
    params = [f"%{w.lower()}%" for w in words]
    return conn.execute(f"""
        SELECT eva, name, stada FROM stations WHERE {where}
        ORDER BY stada IS NULL, length(name), name LIMIT ?""", (*params, limit)).fetchall()


class Nominatim:
    RATE = 1.1

    def __init__(self):
        self._last = 0.0

    def geocode(self, name: str) -> Optional[Tuple[float, float]]:
        for q in (f"Bahnhof {name}", name):
            wait = self.RATE - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = requests.get(NOMINATIM_URL, params={"q": q, "format": "json", "limit": 1, "countrycodes": "de"},
                                 headers={"User-Agent": USER_AGENT}, timeout=10)
                r.raise_for_status()
                res = r.json()
                if res:
                    return float(res[0]["lat"]), float(res[0]["lon"])
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("Nominatim-Fehler für %s: %s", name, e)
        return None
