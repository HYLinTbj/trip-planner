"""Thin Nominatim (OpenStreetMap) geocoding client.

Powers the two "add a POI" entry points: forward geocoding (search box: text →
places) and reverse geocoding (map click: a point → a name to prefill).

Proxied server-side on purpose — Nominatim's usage policy wants a valid,
identifying User-Agent and only light traffic, neither of which a browser can
reliably supply. Same thin-client shape as osrm.py. Config:

    NOMINATIM_URL   default https://nominatim.openstreetmap.org
    NOMINATIM_UA    a contactable User-Agent (Nominatim asks callers to identify)
"""

import os

import httpx

DEFAULT_NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = os.environ.get("NOMINATIM_UA", "trip-planner/0.1 (personal use)")


def _norm(item: dict) -> dict:
    """Reduce a Nominatim record to what the add form needs."""
    return {
        "name": item.get("name") or item.get("display_name", "").split(",")[0],
        "display_name": item.get("display_name", ""),
        "lat": float(item["lat"]),
        "lon": float(item["lon"]),
    }


def search(q: str, limit: int = 5, base_url: str = DEFAULT_NOMINATIM_URL) -> list[dict]:
    """Forward geocode: free text → up to `limit` candidate places."""
    resp = httpx.get(
        f"{base_url}/search",
        params={"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 0},
        headers={"User-Agent": USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    return [_norm(it) for it in resp.json()]


def reverse(lat: float, lon: float, base_url: str = DEFAULT_NOMINATIM_URL,
            detail: bool = False) -> dict:
    """Reverse geocode: a clicked point → a single named place.

    Falls back to the clicked coordinates with an empty name when Nominatim has
    nothing there (e.g. open water) so the add form can still proceed.

    `detail=True` also requests structured address fields and returns them under
    an `address` key (e.g. `ISO3166-2-lvl4` / `state`) — used by places.py to map
    a base coordinate to its US region. The default stays lean for the add form.
    """
    params = {"lat": lat, "lon": lon, "format": "jsonv2"}
    if detail:
        params["addressdetails"] = 1
    resp = httpx.get(
        f"{base_url}/reverse",
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        return {"name": "", "display_name": "", "lat": lat, "lon": lon,
                **({"address": {}} if detail else {})}
    out = _norm(data)
    if detail:
        out["address"] = data.get("address", {})
    return out
