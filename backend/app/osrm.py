"""Thin OSRM client — one self-hosted instance per travel profile.

Given N coordinates, returns an N x N travel-time matrix from OSRM's Table
service. The solver (step 2) consumes this matrix directly.

One OSRM container serves one routing graph (= one mode), so the base URL is
chosen by profile:

    OSRM_FOOT_URL  default http://localhost:5000   (walking graph)
    OSRM_CAR_URL   default http://localhost:5001   (driving graph)
    OSRM_PROFILE   default foot                     (the app's default mode)
    OSRM_URL       optional single override for *all* profiles (e.g. the public
                   demo https://router.project-osrm.org, which only serves driving)
"""

import os

import httpx

OSRM_FOOT_URL = os.environ.get("OSRM_FOOT_URL", "http://localhost:5000")
OSRM_CAR_URL = os.environ.get("OSRM_CAR_URL", "http://localhost:5001")
OSRM_URL_ALL = os.environ.get("OSRM_URL", "")  # if set, used for every profile
DEFAULT_PROFILE = os.environ.get("OSRM_PROFILE", "foot")

_BY_PROFILE = {"foot": OSRM_FOOT_URL, "car": OSRM_CAR_URL, "driving": OSRM_CAR_URL}


def url_for(profile: str | None) -> str:
    """Base URL of the OSRM instance serving `profile` (foot→:5000, car→:5001)."""
    if OSRM_URL_ALL:
        return OSRM_URL_ALL
    return _BY_PROFILE.get(profile or DEFAULT_PROFILE, OSRM_FOOT_URL)


# Back-compat alias (some scripts import this): the default profile's instance.
DEFAULT_OSRM_URL = url_for(DEFAULT_PROFILE)


def table_durations(
    coords: list[tuple[float, float]],
    profile: str | None = None,
    base_url: str | None = None,
) -> list[list[float | None]]:
    """Return an N x N matrix of travel durations in SECONDS for `profile`.

    coords: list of (lat, lon).
    NOTE: OSRM expects coordinates as lon,lat in the URL — a classic footgun.
    """
    profile = profile or DEFAULT_PROFILE
    base = base_url or url_for(profile)
    locs = ";".join(f"{lon},{lat}" for lat, lon in coords)
    url = f"{base}/table/v1/{profile}/{locs}"
    resp = httpx.get(url, params={"annotations": "duration"}, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM returned an error: {data}")
    return data["durations"]


def to_minutes(durations: list[list[float | None]]) -> list[list[float | None]]:
    return [
        [round(s / 60, 1) if s is not None else None for s in row]
        for row in durations
    ]
