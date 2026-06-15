"""Unit tests for the LLM seam — tolerant JSON parsing + the config guard.

No network: the one happy-path call monkeypatches httpx.post.
"""

import pytest

from app import llm
from app.llm import LLMNotConfigured, _loads, _parse, propose_candidates


# --- tolerant JSON extraction ------------------------------------------------

def test_loads_plain_json():
    assert _loads('{"a": 1}') == {"a": 1}


def test_loads_strips_code_fence():
    assert _loads('```json\n{"pois": []}\n```') == {"pois": []}


def test_loads_extracts_object_from_prose():
    raw = 'Sure! Here you go:\n{"pois": [{"name": "X"}]}\nHope that helps.'
    assert _loads(raw) == {"pois": [{"name": "X"}]}


def test_loads_bare_array():
    assert _loads("[1, 2, 3]") == [1, 2, 3]


def test_loads_junk_returns_empty_dict():
    assert _loads("not json at all") == {}
    assert _loads("") == {}


# --- row validation ----------------------------------------------------------

def test_parse_pois_key():
    out = _parse('{"pois": [{"name": "A"}, {"name": "B", "importance": 0.8}]}')
    assert [p.name for p in out] == ["A", "B"]
    assert out[1].importance == 0.8


def test_parse_bare_list():
    assert [p.name for p in _parse('[{"name": "A"}]')] == ["A"]


def test_parse_skips_malformed_rows():
    # the middle row has no `name` (required) -> dropped, valid rows survive
    out = _parse('{"pois": [{"name": "Good"}, {"area": "no name"}, {"name": "Also"}]}')
    assert [p.name for p in out] == ["Good", "Also"]


def test_parse_empty_on_garbage():
    assert _parse("garbage") == []


# --- propose_candidates ------------------------------------------------------

def test_propose_raises_without_model(monkeypatch):
    monkeypatch.setattr(llm, "MODEL", "")
    with pytest.raises(LLMNotConfigured):
        propose_candidates("a trip brief")


def test_propose_happy_path_openai(monkeypatch):
    monkeypatch.setattr(llm, "MODEL", "gpt-test")
    monkeypatch.setattr(llm, "PROVIDER", "openai")
    monkeypatch.setattr(llm, "API_KEY", "sk-test")

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"pois": [{"name": "Museum"}]}'}}]}

    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: FakeResp())
    out = propose_candidates("art trip", area="Denver", count=3)
    assert [p.name for p in out] == ["Museum"]
