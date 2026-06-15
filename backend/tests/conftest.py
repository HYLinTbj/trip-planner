"""Shared fixtures/helpers for the pure-unit suite.

Everything here is offline: no Postgres, no routing engine, no network. Solver tests
build POIs and integer-minute matrices by hand; I/O seams are monkeypatched in the
individual test modules.
"""

from app.models import POI, Hours


def make_poi(
    poi_id: str,
    *,
    name: str | None = None,
    importance: float = 0.5,
    dwell_min: int = 60,
    hours: dict | None = None,
    lat: float = 0.0,
    lon: float = 0.0,
) -> POI:
    """Build a models.POI for solver tests. `hours` accepts either Hours objects or
    plain {"open": "HH:MM", "close": "HH:MM"} dicts, e.g. {"default": {...}}."""
    parsed = None
    if hours is not None:
        parsed = {
            k: v if isinstance(v, Hours) else Hours(**v)
            for k, v in hours.items()
        }
    return POI(
        id=poi_id, name=name or poi_id, lat=lat, lon=lon,
        importance=importance, dwell_min=dwell_min, hours=parsed,
    )


def line_matrix(n: int, gap: int = 10) -> list[list[int]]:
    """An (n+1)x(n+1) symmetric integer-minute matrix (index 0 = base, then n POIs)
    laid out on a line: travel(i, j) = |i - j| * gap, diagonal 0."""
    size = n + 1
    return [[abs(i - j) * gap for j in range(size)] for i in range(size)]
