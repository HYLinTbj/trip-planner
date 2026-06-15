"""Unit tests for the grounding pipeline (candidates.ground + query building).

The geocoder is monkeypatched: grounding must never reach the network, and the
"LLM is not a source of truth" rule means hours stay null regardless of input.
"""

from app import candidates
from app.candidates import _queries, _slug, ground
from app.models import POI, ProposedPOI


# --- query building ----------------------------------------------------------

def test_queries_strips_parenthetical_and_orders():
    p = ProposedPOI(name="Kinkaku-ji (Golden Pavilion)", area="Kyoto")
    qs = _queries(p, None)
    assert qs[0] == "Kinkaku-ji (Golden Pavilion), Kyoto"   # raw name + hint first
    assert "Kinkaku-ji, Kyoto" in qs                        # cleaned name + hint
    assert "Kinkaku-ji (Golden Pavilion)" in qs             # raw name alone
    assert "Kinkaku-ji" in qs                               # cleaned name alone
    assert len(qs) == len(set(qs))                          # de-duplicated


def test_queries_explicit_query_wins():
    p = ProposedPOI(name="Ambiguous", query="explicit lookup", area="A")
    assert _queries(p, None)[0] == "explicit lookup"


# --- grounding ---------------------------------------------------------------

def test_ground_resolved(monkeypatch):
    monkeypatch.setattr(candidates, "geocode_search",
                        lambda q, limit=1: [{"lat": 35.0, "lon": 135.7,
                                             "display_name": "Found, Kyoto"}])
    out = ground([ProposedPOI(name="Temple", area="Kyoto", dwell_min=45)], "Kyoto", {})
    assert len(out) == 1
    c = out[0]
    assert c.status == "resolved"
    assert (c.lat, c.lon) == (35.0, 135.7)
    assert c.display_name == "Found, Kyoto"
    assert c.dwell_min == 45


def test_ground_dwell_defaults_to_60(monkeypatch):
    monkeypatch.setattr(candidates, "geocode_search",
                        lambda q, limit=1: [{"lat": 1, "lon": 2, "display_name": "d"}])
    assert ground([ProposedPOI(name="X")], None, {})[0].dwell_min == 60          # None -> 60
    assert ground([ProposedPOI(name="X", dwell_min=0)], None, {})[0].dwell_min == 60  # <=0 -> 60


def test_ground_unresolved(monkeypatch):
    monkeypatch.setattr(candidates, "geocode_search", lambda q, limit=1: [])
    c = ground([ProposedPOI(name="Nowhere")], None, {})[0]
    assert c.status == "unresolved"
    assert c.lat is None and c.lon is None


def test_ground_flags_duplicate(monkeypatch):
    monkeypatch.setattr(candidates, "geocode_search",
                        lambda q, limit=1: [{"lat": 1, "lon": 2, "display_name": "d"}])
    existing = {"golden-temple": POI(id="golden-temple", name="Golden Temple", lat=1, lon=2)}
    assert ground([ProposedPOI(name="Golden Temple")], None, existing)[0].duplicate is True


def test_ground_swallows_geocode_error_and_tries_next(monkeypatch):
    calls = []

    def flaky(q, limit=1):
        calls.append(q)
        if len(calls) == 1:
            raise RuntimeError("transient geocoder failure")
        return [{"lat": 9, "lon": 9, "display_name": "ok"}]

    monkeypatch.setattr(candidates, "geocode_search", flaky)
    c = ground([ProposedPOI(name="Place", area="City")], None, {})[0]   # yields >=2 variants
    assert c.status == "resolved"
    assert c.lat == 9
    assert len(calls) >= 2          # first variant raised, second succeeded


def test_slug():
    assert _slug("Golden Temple!") == "golden-temple"
    assert _slug("  Hello  World  ") == "hello-world"
