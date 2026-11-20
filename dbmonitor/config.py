"""Configuration from the .env file (single source of truth).

ROUTES syntax (EVA numbers, optional label in parentheses):
    ROUTES=8001920(Esslingen)<->8000096(Stuttgart Hbf);8001920->8000302
Legacy syntax with station names is still accepted (resolved by the collector):
    POLLING_ROUTES=Berlin Hbf<->Hamburg Hbf
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_ENV = Path(os.environ.get("DBMON_ENV", Path(__file__).resolve().parent.parent / ".env"))

_TOKEN = re.compile(r"^\s*(\d{6,9})\s*(?:\((.*)\))?\s*$")


@dataclass(frozen=True)
class StationRef:
    eva: Optional[str]          # set if given as number
    label: Optional[str]        # optional name
    name: Optional[str] = None  # legacy: only a name was given

    def display(self) -> str:
        return self.label or self.name or self.eva or "?"


@dataclass(frozen=True)
class RouteSpec:
    origin: StationRef
    dest: StationRef


@dataclass
class Settings:
    client_id: str = ""
    api_key: str = ""
    db_path: str = "./train_db.db"
    live_interval: int = 600
    finalize_at: dt.time = dt.time(3, 0)
    backfill_days: int = 29
    geocoding: bool = True
    routes: List[RouteSpec] = field(default_factory=list)
    stations: List[StationRef] = field(default_factory=list)   # STATIONS: observed on their own
    routes_configured: bool = False   # False: no ROUTES key at all -> keep routes from the DB
    route_errors: List[str] = field(default_factory=list)
    env_path: Optional[Path] = None
    env_mtime: float = 0.0


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        values[k.strip()] = v
    return values


def _parse_token(tok: str) -> StationRef:
    m = _TOKEN.match(tok)
    if m:
        return StationRef(eva=m.group(1), label=(m.group(2) or "").strip() or None)
    name = tok.strip()
    if not name:
        raise ValueError("leerer Bahnhof")
    return StationRef(eva=None, label=None, name=name)


def parse_routes(text: str) -> Tuple[List[RouteSpec], List[str]]:
    """Parse ROUTES. Returns (routes, errors). Duplicates are removed, order is kept."""
    routes: List[RouteSpec] = []
    errors: List[str] = []
    seen = set()
    for raw in (text or "").split(";"):
        item = raw.strip()
        if not item:
            continue
        try:
            if "<->" in item:
                a, b = item.split("<->", 1)
                pairs = [(_parse_token(a), _parse_token(b))]
                pairs.append((pairs[0][1], pairs[0][0]))
            elif "->" in item:
                a, b = item.split("->", 1)
                pairs = [(_parse_token(a), _parse_token(b))]
            else:
                raise ValueError("'->' oder '<->' fehlt")
        except ValueError as e:
            errors.append(f"'{item}': {e}")
            continue
        for o, d in pairs:
            key = (o.eva or o.name, d.eva or d.name)
            if key[0] == key[1]:
                errors.append(f"'{item}': Start und Ziel sind gleich")
                continue
            if key not in seen:
                seen.add(key)
                routes.append(RouteSpec(o, d))
    return routes, errors


def parse_stations(text: str) -> Tuple[List[StationRef], List[str]]:
    """STATIONS=8006642(Esslingen-Zell);8005769 – separated by ';'."""
    out, errors, seen = [], [], set()
    for raw in (text or "").split(";"):
        if not raw.strip():
            continue
        try:
            ref = _parse_token(raw)
        except ValueError as e:
            errors.append(f"STATIONS '{raw.strip()}': {e}")
            continue
        key = ref.eva or ref.name
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out, errors


def format_stations(items: List[Tuple[str, str]]) -> str:
    return ";".join(f"{e}({_sanitize_label(n)})" if n else e for e, n in items)


def format_routes(pairs: List[Tuple[str, str, str, str]]) -> str:
    """pairs: (origin_eva, origin_name, dest_eva, dest_name). Merges both directions into '<->'."""
    names: Dict[str, str] = {}
    ordered: List[Tuple[str, str]] = []
    for o, on, d, dn in pairs:
        names[o], names[d] = on, dn
        if (o, d) not in ordered:
            ordered.append((o, d))
    out, done = [], set()
    for o, d in ordered:
        if (o, d) in done:
            continue
        tok = lambda e: f"{e}({names[e]})" if names.get(e) else e  # noqa: E731
        if (d, o) in ordered:
            out.append(f"{tok(o)}<->{tok(d)}")
            done.update({(o, d), (d, o)})
        else:
            out.append(f"{tok(o)}->{tok(d)}")
            done.add((o, d))
    return ";".join(out)


def _sanitize_label(s: str) -> str:
    return re.sub(r"[;()=\n\r<>]", " ", s).strip()


def write_routes(path: Path, pairs: List[Tuple[str, str, str, str]]) -> str:
    """Replace the ROUTES= line (legacy POLLING_ROUTES is commented out). Returns the new value."""
    clean = [(o, _sanitize_label(on), d, _sanitize_label(dn)) for o, on, d, dn in pairs]
    value = format_routes(clean)
    write_env_value(path, "ROUTES", value, comment="Beobachtete Strecken (EVA-Nummern), gepflegt mit: dbmon",
                    supersedes="POLLING_ROUTES")
    return value


def write_stations(path: Path, items: List[Tuple[str, str]]) -> str:
    value = format_stations(items)
    write_env_value(path, "STATIONS", value, comment="Zusätzlich beobachtete Bahnhöfe (EVA-Nummern), gepflegt mit: dbmon")
    return value


def write_env_value(path: Path, key: str, value: str, comment: str = "", supersedes: Optional[str] = None) -> None:
    """Replace KEY=... (or append it). Keeps every other line, writes a .env.bak backup when possible
    and replaces the file atomically when the directory is writable."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_lines, replaced = [], False
    for line in lines:
        st = line.strip()
        if st.startswith(f"{key}="):
            if not replaced:
                new_lines.append(f"{key}={value}")
                replaced = True
            continue
        if supersedes and st.startswith(f"{supersedes}="):
            new_lines.append(f"# {st}   # ersetzt durch {key} (dbmon)")
            continue
        new_lines.append(line)
    if not replaced:
        new_lines += ["", f"# {comment}" if comment else "", f"{key}={value}"]
    content = "\n".join(new_lines) + "\n"
    if path.exists():
        try:
            path.with_name(path.name + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            # e.g. Docker: only the file itself is mounted writable -> keep the old line visible instead
            old = next((l for l in lines if l.strip().startswith(f"{key}=")), None)
            if old:
                print(f"(Keine .env.bak möglich – bisherige Zeile:)\n{old}\n")
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    except OSError:
        tmp = None
    if tmp is None:
        with open(path, "w", encoding="utf-8") as fh:  # directory not writable: write in place
            fh.write(content)
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(tmp, (path.stat().st_mode & 0o777) if path.exists() else 0o600)
    try:
        os.replace(tmp, path)
    except OSError:
        # single-file bind mount cannot be replaced atomically -> write in place
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.unlink(tmp)


def load_settings(env_path: Optional[Path] = None) -> Settings:
    path = Path(env_path or DEFAULT_ENV)
    file_vals = read_env_file(path)

    def get(key: str, default: str = "") -> str:
        # values from the file win so that edits take effect without restart;
        # real environment variables are the fallback (e.g. Docker secrets)
        return file_vals.get(key, os.environ.get(key, default))

    s = Settings(env_path=path, env_mtime=path.stat().st_mtime if path.exists() else 0.0)
    s.client_id = get("DB_CLIENT_ID")
    s.api_key = get("DB_API_KEY")
    # DB_PATH: the process environment wins (deployment-specific, e.g. /data in Docker);
    # a relative path is resolved against the directory of the .env file
    db_path = os.environ.get("DB_PATH") or file_vals.get("DB_PATH") or s.db_path
    s.db_path = str((path.parent / db_path).resolve()) if not os.path.isabs(db_path) else db_path
    try:
        s.live_interval = max(120, int(get("LIVE_INTERVAL", "600")))
    except ValueError:
        s.route_errors.append("LIVE_INTERVAL ist keine Zahl, verwende 600")
    try:
        hh, mm = get("FINALIZE_AT", "03:00").split(":")
        s.finalize_at = dt.time(int(hh), int(mm))
    except ValueError:
        s.route_errors.append("FINALIZE_AT ungültig (HH:MM), verwende 03:00")
    try:
        s.backfill_days = min(29, max(0, int(get("BACKFILL_DAYS", "29"))))
    except ValueError:
        pass
    s.geocoding = get("GEOCODING", "nominatim").strip().lower() not in ("off", "0", "false", "no", "aus")
    present = any(k in file_vals or k in os.environ for k in ("ROUTES", "POLLING_ROUTES"))
    s.routes_configured = present
    routes_text = get("ROUTES") or get("POLLING_ROUTES")
    routes, errors = parse_routes(routes_text)
    s.routes = routes
    s.route_errors += errors
    s.stations, st_errors = parse_stations(get("STATIONS"))
    s.route_errors += st_errors
    return s
