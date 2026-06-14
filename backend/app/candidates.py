"""Grounding pipeline: turn raw LLM ideas (`ProposedPOI`) into staged `Candidate`s.

This is where "the LLM is not a source of truth" is enforced: every proposed name
is located by geocoding (`geocode.search` → Nominatim), never by trusting the
model, and opening hours are left null (filled later from OSM/curation, never the
LLM). Reuses the same geocoder the manual "add a POI" flow uses.
"""

import re

from .geocode import search as geocode_search
from .models import POI, Candidate, ProposedPOI


def _queries(p: ProposedPOI, area: str | None) -> list[str]:
    """Geocoding attempts in priority order. Names often carry a parenthetical
    qualifier (e.g. "Kinkaku-ji (Golden Pavilion)") that defeats Nominatim, so we
    fall back to a cleaned name and to dropping the area hint."""
    hint = p.area or area
    name = p.name
    clean = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()  # strip "(...)" qualifiers
    variants = []
    if p.query:
        variants.append(p.query)
    if hint:
        variants.append(f"{name}, {hint}")
        if clean and clean != name:
            variants.append(f"{clean}, {hint}")
    variants.append(name)
    if clean and clean != name:
        variants.append(clean)
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def ground(proposed: list[ProposedPOI], area: str | None, existing: dict[str, POI]) -> list[Candidate]:
    """Geocode each idea, flag the ones that don't resolve or already exist."""
    existing_slugs = set(existing) | {_slug(p.name) for p in existing.values()}
    out: list[Candidate] = []
    for p in proposed:
        hits = []
        for q in _queries(p, area):
            try:
                hits = geocode_search(q, limit=1)
            except Exception:
                hits = []  # a flaky geocode shouldn't drop the idea — try the next variant
            if hits:
                break
        dwell = p.dwell_min if (p.dwell_min and p.dwell_min > 0) else 60
        c = Candidate(
            name=p.name, importance=p.importance, dwell_min=dwell,
            tags=p.tags, rationale=p.rationale,
            duplicate=_slug(p.name) in existing_slugs,
        )
        if hits:
            h = hits[0]
            c.lat, c.lon, c.display_name, c.status = h["lat"], h["lon"], h["display_name"], "resolved"
        else:
            c.status = "unresolved"
        out.append(c)
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
