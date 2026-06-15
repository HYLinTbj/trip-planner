"""Unit tests for the Pydantic API models — mainly POICreate.to_poi's hours logic
(the grounding invariant: hours only when the add form supplies BOTH open and close).
"""

from app.models import POICreate, ProposedPOI


def test_to_poi_builds_default_hours_window():
    p = POICreate(name="Cafe", lat=1.0, lon=2.0, open="09:00", close="17:00").to_poi("cafe")
    assert p.id == "cafe"
    assert p.hours is not None
    assert p.hours["default"].open == "09:00"
    assert p.hours["default"].close == "17:00"


def test_to_poi_partial_hours_stays_none():
    assert POICreate(name="X", lat=0, lon=0, open="09:00").to_poi("x").hours is None
    assert POICreate(name="X", lat=0, lon=0, close="17:00").to_poi("x").hours is None


def test_to_poi_no_hours():
    assert POICreate(name="X", lat=0, lon=0).to_poi("x").hours is None


def test_to_poi_passes_through_fields():
    p = POICreate(name="Park", lat=1.0, lon=2.0, dwell_min=90,
                  importance=0.8, tags=["nature"], notes="nice").to_poi("park")
    assert p.dwell_min == 90
    assert p.importance == 0.8
    assert p.tags == ["nature"]
    assert p.notes == "nice"


def test_proposed_poi_defaults():
    p = ProposedPOI(name="X")
    assert p.importance == 0.5
    assert p.tags == []
    assert p.dwell_min is None
    assert p.area is None
