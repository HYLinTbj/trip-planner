"""US census-region resolution for free-base trips.

The four regional Valhalla engines (data/regions.json) partition the contiguous USA by
US census region. To let a user base a trip at *any* place we must pick the engine for
that place — which is deterministic from its state. STATE_REGION below is aligned to the
**Geofabrik tile footprints** those engines are built from (scripts/build_engine.py), not
just census theory: Geofabrik's `us-west` PBF excludes Alaska and Hawaii (they ship as
separate extracts), so AK/HI map to None ("unsupported") rather than `west`. Anything
outside the table — non-US, or a point with no resolvable state — is None too, and the
caller surfaces a clear "outside coverage" error instead of mis-routing.

The region keys here MUST match the keys in data/regions.json.
"""

from . import geocode

# USPS state code -> regional engine key (data/regions.json). AK/HI omitted on purpose
# (not in the us-west extract); they resolve to None and read as "outside coverage".
_NORTHEAST = {"CT", "ME", "MA", "NH", "NJ", "NY", "PA", "RI", "VT"}
_MIDWEST = {"IA", "IL", "IN", "KS", "MI", "MN", "MO", "ND", "NE", "OH", "SD", "WI"}
_SOUTH = {"AL", "AR", "DC", "DE", "FL", "GA", "KY", "LA", "MD", "MS",
          "NC", "OK", "SC", "TN", "TX", "VA", "WV"}
_WEST = {"AZ", "CA", "CO", "ID", "MT", "NM", "NV", "OR", "UT", "WA", "WY"}

STATE_REGION: dict[str, str] = {
    **{s: "northeast" for s in _NORTHEAST},
    **{s: "midwest" for s in _MIDWEST},
    **{s: "south" for s in _SOUTH},
    **{s: "west" for s in _WEST},
}

# Lower-cased full state name -> USPS code, so a Nominatim `state` ("Colorado") resolves
# even when the structured ISO code is absent. Includes AK/HI (they map to a code that is
# deliberately not in STATE_REGION, so they still come back None).
STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def region_for_state(value: str | None) -> str | None:
    """A USPS code ('CO'), ISO 3166-2 ('US-CO'), or full state name ('Colorado') →
    regional engine key, or None when it isn't a supported US state."""
    if not value:
        return None
    code = value.strip().upper()
    if code.startswith("US-"):
        code = code[3:]
    if code in STATE_REGION:
        return STATE_REGION[code]
    return STATE_REGION.get(STATE_NAMES.get(value.strip().lower(), ""))


def region_for_point(lat: float, lon: float) -> str | None:
    """Reverse-geocode a base coordinate to its US state, then its regional engine key.
    None when the point isn't in a supported US region (non-US, or AK/HI)."""
    info = geocode.reverse(lat, lon, detail=True)
    addr = info.get("address") or {}
    return region_for_state(addr.get("ISO3166-2-lvl4")) or region_for_state(addr.get("state"))


def region_for_points(points: list[tuple[float, float]]) -> str | None:
    """The single US region covering every point, or None when they fall outside coverage
    or span more than one region — the within-region constraint for route trips (HYL-68).
    A trip whose anchors/POIs cross regions can't be matrixed by one regional engine yet."""
    regions = {region_for_point(lat, lon) for lat, lon in points}
    return regions.pop() if len(regions) == 1 else None
