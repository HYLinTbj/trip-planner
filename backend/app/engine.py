"""Routing-engine selector — the single seam the matrix builder and API import.

Both backends expose a compatible `table_durations(coords, profile)` (seconds) and
`to_minutes`. Valhalla is the default (one instance, all modes via `costing`); OSRM
is kept as a fallback (one instance per profile). Flip with no code change:

    ROUTING_ENGINE   valhalla (default) | osrm
    ROUTE_PROFILE    default mode when a request gives none (default: foot)
"""

import os

ROUTING_ENGINE = os.environ.get("ROUTING_ENGINE", "valhalla").lower()
DEFAULT_PROFILE = os.environ.get("ROUTE_PROFILE", "foot")

if ROUTING_ENGINE == "osrm":
    from . import osrm as _engine

    def base_url(profile: str | None = None, region: str | None = None) -> str:
        """OSRM serves one profile per instance, so the URL depends on the mode.
        (Regional engines are a Valhalla-only concept; `region` is ignored.)"""
        return _engine.url_for(profile)
else:
    from . import valhalla as _engine

    def base_url(profile: str | None = None, region: str | None = None) -> str:
        """Valhalla serves every mode from one instance; pick the region's engine
        (data/regions.json). Unknown/None region -> the default VALHALLA_URL."""
        return _engine.url_for_region(region)


# Re-exported so matrix.py / main.py never import a concrete engine module.
table_durations = _engine.table_durations
to_minutes = _engine.to_minutes
route_geometry = _engine.route_geometry   # HYL-70: decoded (lat, lon) road path for a leg chain
