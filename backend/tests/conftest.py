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


def matrix_from_positions(positions: list[float], gap: int = 10) -> list[list[int]]:
    """Symmetric integer-minute matrix from 1-D positions: travel(i, j) = |posᵢ-posⱼ|*gap."""
    return [[int(abs(a - b) * gap) for b in positions] for a in positions]


def base_line(num_days: int, poi_positions: list[float], gap: int = 10):
    """(day_anchors, matrix) for a single-base trip on a line: the base (position 0)
    duplicated into 2*num_days co-located anchor nodes, then POIs at `poi_positions`
    (same order as the `pois` list passed to plan_trip). Mirrors how main._run lays out
    a base-mode solve — every day starts and ends at the base."""
    day_anchors = [(2 * i, 2 * i + 1) for i in range(num_days)]
    positions = [0.0] * (2 * num_days) + list(poi_positions)
    return day_anchors, matrix_from_positions(positions, gap)
