"""Core data models.

Design note: a POI is a *persistent, user-owned* entity, not trip-scoped
scratch data. Trips will reference POIs by id (added in step 2), so the
`tags` field below is the seam that grows into the personal POI library
(stash + tag + filter). It is unused by the spike, but present from day one
so the library feature is purely additive later — not a migration.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Hours(BaseModel):
    open: str   # "HH:MM"
    close: str  # "HH:MM"


class POI(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    dwell_min: int = 60
    # Baseline desire to visit (0..1). Used by the solver as the drop-penalty,
    # so low-importance POIs are shed first when a day can't hold everything.
    # A trip may override this per its candidate pool later.
    importance: float = 0.5
    # Keyed by weekday ("mon".."sun") or "default". Parsed/enforced by the
    # solver in step 2 — the spike only needs coordinates.
    hours: Optional[dict[str, Hours]] = None
    # User-authored, free-form. Foundation for the library's tag filtering.
    tags: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    status: Literal["idea", "visited"] = "idea"


class Lock(BaseModel):
    """A user edit the solver must honor — the 'you dispose' half of the app."""
    poi_id: str
    type: Literal["day", "exclude", "include", "pin"]
    day: Optional[int] = None    # 0-based day index, for "day" / "pin"
    time: Optional[str] = None   # "HH:MM" arrival, for "pin" (a reservation)


class POICreate(BaseModel):
    """Inbound payload for adding a POI to the library (the id is server-assigned).

    `open`/`close` are a convenience for the add form: when both are set they
    become the POI's single `hours["default"]` window; otherwise hours stay
    unset (the solver then treats the POI as open across the whole day).
    """
    name: str
    lat: float
    lon: float
    dwell_min: int = 60
    importance: float = 0.5
    open: Optional[str] = None    # "HH:MM"
    close: Optional[str] = None   # "HH:MM"
    tags: list[str] = Field(default_factory=list)
    notes: Optional[str] = None

    def to_poi(self, poi_id: str) -> POI:
        hours = (
            {"default": Hours(open=self.open, close=self.close)}
            if self.open and self.close else None
        )
        return POI(
            id=poi_id, name=self.name, lat=self.lat, lon=self.lon,
            dwell_min=self.dwell_min, importance=self.importance,
            hours=hours, tags=self.tags, notes=self.notes,
        )


class ProposedPOI(BaseModel):
    """One raw idea from the LLM — names + soft metadata only.

    Deliberately carries NO opening hours and treats any coordinates as untrusted:
    location is established downstream by geocoding (see `candidates.ground`), and
    hours come only from OSM/curation. The model is an idea generator, never a
    source of truth — this schema is how that rule is enforced.
    """
    name: str
    area: Optional[str] = None        # city/region hint to disambiguate geocoding
    query: Optional[str] = None       # explicit geocoding query, if the name is ambiguous
    importance: float = 0.5
    dwell_min: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    rationale: Optional[str] = None   # one-line "why this fits the brief"


class Candidate(BaseModel):
    """A `ProposedPOI` after grounding: geocoded (or flagged) and staged for the
    user to accept. Still no hours — those stay null until OSM/curation fills them."""
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    display_name: Optional[str] = None
    importance: float = 0.5
    dwell_min: int = 60
    tags: list[str] = Field(default_factory=list)
    rationale: Optional[str] = None
    status: Literal["resolved", "unresolved"] = "resolved"
    duplicate: bool = False


class SuggestRequest(BaseModel):
    """A natural-language trip brief, plus an optional area hint for geocoding."""
    prompt: str
    area: Optional[str] = None
    count: int = 8
