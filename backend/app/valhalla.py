"""Valhalla routing client — the app's default engine (used behind engine.py).

Mirrors `osrm.table_durations`: same (lat, lon) input, same SECONDS-matrix output.
A single Valhalla instance serves every mode via the `costing` parameter, so one URL
covers foot/car/transit alike (unlike OSRM's instance-per-profile). For the scale-up
there are several REGIONAL engines (one per US census region, data/regions.json); the
api picks the right one per request via `url_for_region` (see engine.base_url).

    VALHALLA_URL          default http://localhost:8002 (fallback / non-US cities)
    VALHALLA_REGION_HOST  host of the regional engines (default localhost)
"""

import datetime
import json
import os
from pathlib import Path

import httpx

from .polyline import decode_polyline

VALHALLA_URL = os.environ.get("VALHALLA_URL", "http://localhost:8002")

# Region -> engine URL registry (scale-up phase 2). Each regional Valhalla serves on
# its own port (data/regions.json); the api routes each request to the engine for the
# city's census region. An unknown/None region falls back to VALHALLA_URL — e.g. the
# legacy per-city engine for non-US cities like kyoto.
#
# Two address modes:
#   dev (default):   http://<VALHALLA_REGION_HOST>:<port>   (engines published on host)
#   containerized:   VALHALLA_REGION_URL_TEMPLATE, e.g. "http://valhalla-{region}:8002"
#                    (Phase 4 compose: reach each engine by its service name)
_REGIONS_FILE = Path(__file__).resolve().parents[2] / "data" / "regions.json"
_REGION_HOST = os.environ.get("VALHALLA_REGION_HOST", "localhost")
_REGION_URL_TMPL = os.environ.get("VALHALLA_REGION_URL_TEMPLATE", "")


def _load_region_urls() -> dict[str, str]:
    try:
        regions = json.loads(_REGIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if _REGION_URL_TMPL:
        return {name: _REGION_URL_TMPL.format(region=name) for name in regions}
    return {name: f"http://{_REGION_HOST}:{r['port']}" for name, r in regions.items()}


REGION_URLS = _load_region_urls()


def url_for_region(region: str | None) -> str:
    """Engine URL for a city's region; falls back to VALHALLA_URL when unknown."""
    return REGION_URLS.get(region or "", VALHALLA_URL)
# Transit is time-dependent, so its matrix is built at one representative departure.
# ISO local time; empty -> next weekday 10:00. Must fall in the GTFS service window.
VALHALLA_DEPART = os.environ.get("VALHALLA_DEPART", "")

# Snap radius (m) applied to every matrix/route location. A coordinate that lands on
# a pedestrian-interior or disconnected edge (parks, museums, malls) otherwise yields
# NO route under `auto` — e.g. Denver Botanic Gardens came back unreachable by car —
# so we let Valhalla search outward to the nearest routable road. 0 = Valhalla default.
SNAP_RADIUS = int(os.environ.get("VALHALLA_SNAP_RADIUS", "200"))

# Our profile names -> Valhalla costing models.
_COSTING = {"foot": "pedestrian", "car": "auto", "bicycle": "bicycle", "transit": "multimodal"}


def costing_for(profile: str | None) -> str:
    return _COSTING.get(profile or "foot", "pedestrian")


def _loc(lat: float, lon: float) -> dict:
    """A Valhalla location, with our snap radius so off-road coordinates still route."""
    loc = {"lat": lat, "lon": lon}
    if SNAP_RADIUS:
        loc["radius"] = SNAP_RADIUS
    return loc


def table_durations(
    coords: list[tuple[float, float]],
    profile: str | None = None,
    base_url: str = VALHALLA_URL,
) -> list[list[float | None]]:
    """Return an N x N matrix of travel durations in SECONDS for `profile`.

    coords: list of (lat, lon). foot/car/bike use the fast /sources_to_targets
    matrix; **transit** (multimodal) is assembled from per-pair /route calls,
    because Valhalla's matrix endpoint can't do multimodal. Cells with no route
    come back as None.
    """
    costing = costing_for(profile)
    if costing == "multimodal":
        return _transit_matrix(coords, base_url)
    pts = [_loc(lat, lon) for lat, lon in coords]
    body = {"sources": pts, "targets": pts, "costing": costing}
    try:
        resp = httpx.post(f"{base_url}/sources_to_targets", json=body, timeout=60.0)
        resp.raise_for_status()
        rows = resp.json()["sources_to_targets"]
        return [[cell.get("time") for cell in row] for row in rows]
    except httpx.HTTPStatusError:
        # The matrix endpoint can crash on some inputs even when routing is fine — e.g.
        # a wide pedestrian expansion sweeping a malformed edge ("GetTags: offset
        # exceeds size of text list", 500). Per-pair /route avoids the matrix code path:
        # slower (N^2 calls) but correct. (Connection errors are NOT caught here — they
        # propagate so the API can report the engine as unreachable.)
        return _pairwise_matrix(coords, base_url, costing)


def to_minutes(durations: list[list[float | None]]) -> list[list[float | None]]:
    return [
        [round(s / 60, 1) if s is not None else None for s in row]
        for row in durations
    ]


def _depart() -> str:
    """ISO local departure time for transit queries (representative weekday 10:00)."""
    if VALHALLA_DEPART:
        return VALHALLA_DEPART
    d = datetime.date.today()
    while d.weekday() >= 5:          # roll Sat/Sun forward to Monday
        d += datetime.timedelta(days=1)
    return f"{d.isoformat()}T10:00"


def _route_time(a, b, base_url: str, costing: str = "multimodal",
                depart: str | None = None) -> float | None:
    """Travel time in SECONDS for one A->B pair via /route (avoids the matrix endpoint)."""
    body = {"locations": [_loc(a[0], a[1]), _loc(b[0], b[1])], "costing": costing}
    if depart:
        body["date_time"] = {"type": 1, "value": depart}   # type 1 = depart at
    try:
        resp = httpx.post(f"{base_url}/route", json=body, timeout=30.0)
        resp.raise_for_status()
        return resp.json()["trip"]["summary"]["time"]
    except Exception:
        return None                  # no route -> solver treats the leg as unreachable


def _route_shape(locations: list[tuple[float, float]], base_url: str, costing: str,
                 depart: str | None) -> list[tuple[float, float]] | None:
    """Decoded (lat, lon) path for ONE /route call over `locations`, or None on any error.
    Valhalla returns a `shape` (encoded polyline, precision 6) per leg; decode each and
    concatenate, dropping the point each leg shares with the previous one."""
    body = {"locations": [_loc(lat, lon) for lat, lon in locations], "costing": costing}
    if depart:
        body["date_time"] = {"type": 1, "value": depart}     # transit needs a departure
    try:
        resp = httpx.post(f"{base_url}/route", json=body, timeout=60.0)
        resp.raise_for_status()
        legs = resp.json()["trip"]["legs"]
    except Exception:
        return None
    path: list[tuple[float, float]] = []
    for leg in legs:
        pts = decode_polyline(leg["shape"])
        if path and pts and path[-1] == pts[0]:
            pts = pts[1:]                     # drop the seam shared with the previous leg
        path.extend(pts)
    return path


def route_geometry(
    coords: list[tuple[float, float]],
    profile: str | None = None,
    base_url: str = VALHALLA_URL,
) -> list[tuple[float, float]] | None:
    """Decoded (lat, lon) road path for the ordered `coords` (start → … → end), or None if
    nothing could be routed (HYL-70 route visualization).

    Fast path: one /route over all the day's waypoints. But the **pedestrian** costing can 500 a
    multi-leg route when its expansion sweeps a malformed edge ("GetTags: offset exceeds size of
    text list" — the same Valhalla bug matrix.py works around for /sources_to_targets), so on
    failure we fall back to per-leg /route calls, retrying a failed symmetric-mode leg in reverse
    (the bug is directional) and flipping the shape; a leg that fails both ways degrades to a
    straight segment, keeping the path continuous. Mirrors table_durations' batch→pairwise
    fallback. Returns None only when no leg yields real geometry (caller then draws a straight
    line for the whole day).
    """
    if len(coords) < 2:
        return None
    costing = costing_for(profile)
    depart = _depart() if costing == "multimodal" else None
    path = _route_shape(coords, base_url, costing, depart)
    if path:
        return path
    # Per-leg fallback. For symmetric modes (foot/bicycle) the bug is directional — a leg that
    # 500s one way usually routes the other way — so retry reversed and flip the shape, the same
    # trick matrix._repair_unreachable uses for the matrix.
    symmetric = costing in ("pedestrian", "bicycle")
    out: list[tuple[float, float]] = []
    any_real = False
    for a, b in zip(coords, coords[1:]):
        leg = _route_shape([a, b], base_url, costing, depart)
        if not leg and symmetric:
            rev = _route_shape([b, a], base_url, costing, depart)
            if rev:
                leg = rev[::-1]               # same leg walked backwards
        if leg:
            any_real = True
            if out and out[-1] == leg[0]:
                leg = leg[1:]
            out.extend(leg)
        else:                                 # both directions failed -> straight to the next waypoint
            if not out:
                out.append(a)
            out.append(b)
    return out if any_real else None


def _pairwise_matrix(coords, base_url: str, costing: str,
                     depart: str | None = None) -> list[list[float | None]]:
    """Assemble an N x N matrix from per-pair /route calls. Used for transit (the matrix
    endpoint can't do multimodal) and as the fallback when /sources_to_targets errors on
    a costing. Diagonal is 0; an unroutable pair is None."""
    n = len(coords)
    out = [[0.0 if i == j else None for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                out[i][j] = _route_time(coords[i], coords[j], base_url, costing, depart)
    return out


def _transit_matrix(coords, base_url: str) -> list[list[float | None]]:
    """N x N transit matrix from per-pair multimodal /route calls (Valhalla's matrix
    endpoint doesn't support multimodal). One representative departure for the whole
    matrix — a documented approximation; bounded by the active POIs."""
    return _pairwise_matrix(coords, base_url, "multimodal", _depart())
