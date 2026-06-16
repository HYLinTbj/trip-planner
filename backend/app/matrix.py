"""Travel-time matrix builder with a tiny on-disk cache.

The solver wants an integer-minute matrix over [anchor/depot nodes…, *pois]. The routing engine
is the source of truth, but remote ones are rate-limited, so we cache by a hash of
the coordinates, profile, and backend URL to keep repeated solver runs offline.
(The URL is in the key so the same profile name against a different engine — e.g.
Valhalla :8002 vs OSRM :5000, or foot vs car — can't return stale cross-backend data.)
"""

import hashlib
import json
from pathlib import Path

from .engine import DEFAULT_PROFILE, base_url as engine_base_url, table_durations

UNREACHABLE = 10**6  # minutes; effectively bars an arc OSRM couldn't route

# Modes where A->B and B->A are essentially the same trip (no one-way streets).
_SYMMETRIC = {"foot", "bicycle"}


def _repair_unreachable(m: list[list[int]]) -> None:
    """In place: fill a one-directional UNREACHABLE arc from its reachable reverse.

    For symmetric modes an arc that routes one way but not the other is a routing
    glitch (e.g. a malformed edge that breaks B->A but not A->B). Left alone it strands
    the POI — you can't return to base from it — so the solver must drop it, cascading
    one bad edge into many dropped stops. You can always walk/bike back the way you came,
    so mirror the reachable direction. Genuinely far POIs stay UNREACHABLE both ways."""
    n = len(m)
    for i in range(n):
        for j in range(n):
            if m[i][j] >= UNREACHABLE and m[j][i] < UNREACHABLE:
                m[i][j] = m[j][i]


def _key(coords: list[tuple[float, float]], profile: str, base_url: str) -> str:
    payload = json.dumps(
        [[round(lat, 6), round(lon, 6)] for lat, lon in coords] + [profile, base_url],
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()


def get_matrix_min(
    coords: list[tuple[float, float]],
    profile: str | None = None,
    cache_path: str | Path | None = None,
    base_url: str | None = None,
) -> list[list[int]]:
    """Return an N x N integer-minute matrix (anchor/depot nodes first, then the POIs).

    base_url pins which routing engine to query (the city's regional Valhalla); it is
    also part of the cache key, so a city served by a different region can't collide.
    """
    cache: dict = {}
    if cache_path and Path(cache_path).exists():
        cache = json.loads(Path(cache_path).read_text())
    url = base_url or engine_base_url(profile)
    key = _key(coords, profile or DEFAULT_PROFILE, url)
    if key in cache:
        return cache[key]

    seconds = (table_durations(coords, profile=profile, base_url=url) if profile
               else table_durations(coords, base_url=url))
    matrix = [
        [int(round(s / 60)) if s is not None else UNREACHABLE for s in row]
        for row in seconds
    ]
    if (profile or DEFAULT_PROFILE) in _SYMMETRIC:
        _repair_unreachable(matrix)
    if cache_path:
        cache[key] = matrix
        Path(cache_path).write_text(json.dumps(cache))
    return matrix
