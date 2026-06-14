"""LLM provider seam — turns a trip brief into candidate place *names*.

The model is an idea generator only: it returns names + tags + importance + a
one-line rationale. It must NOT invent opening hours or coordinates — those are
grounded downstream (`candidates.ground` geocodes via Nominatim; hours come from
OSM/curation). Thin httpx client like `osrm.py` / `geocode.py`, no SDK dependency,
so one code path drives OpenAI, Groq, Together, OpenRouter, Mistral and local
Ollama (all OpenAI-compatible) plus Anthropic (Claude).

Config via env (load from a gitignored `.env`; `scripts/serve.sh` auto-sources it):
    LLM_PROVIDER   openai (default) | anthropic
    LLM_MODEL      e.g. gpt-4o-mini, llama-3.3-70b-versatile, claude-haiku-4-5-20251001
    LLM_API_KEY    your key (omit for local Ollama)
    LLM_BASE_URL   override endpoint, e.g. https://api.groq.com/openai/v1
                   or http://localhost:11434/v1 (Ollama) or https://api.anthropic.com
"""

import json
import os

import httpx

from .models import ProposedPOI

PROVIDER = os.environ.get("LLM_PROVIDER", "openai").lower()
MODEL = os.environ.get("LLM_MODEL", "")
API_KEY = os.environ.get("LLM_API_KEY", "")
BASE_URL = os.environ.get("LLM_BASE_URL", "")

_DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}

SYSTEM = (
    "You suggest real, visitable points of interest for a trip. "
    'Return ONLY a JSON object of the form {"pois": [...]}, where each item has: '
    "name (the commonly used place name), area (city/region, to aid map lookup), "
    "importance (number 0-1), dwell_min (integer minutes for a typical visit), "
    "tags (array of short lowercase strings), and rationale (one short sentence on "
    "why it fits the request). Suggest places that actually exist and match the "
    "brief's location, season, and constraints. DO NOT invent opening hours. "
    "DO NOT output coordinates. Avoid duplicating places the user already has."
)


class LLMNotConfigured(RuntimeError):
    """Raised when no usable provider/model/key is configured (→ HTTP 503)."""


def _base_url() -> str:
    return BASE_URL or _DEFAULT_BASE.get(PROVIDER, _DEFAULT_BASE["openai"])


def _is_local() -> bool:
    url = _base_url()
    return "localhost" in url or "127.0.0.1" in url


def propose_candidates(prompt, area=None, count=8, existing_names=None) -> list[ProposedPOI]:
    """Ask the configured model for ~`count` place ideas matching `prompt`."""
    if not MODEL:
        raise LLMNotConfigured("Set LLM_MODEL (and usually LLM_API_KEY). See README — AI suggestions.")
    existing = ", ".join(existing_names or []) or "(none yet)"
    user = (
        f"Trip brief: {prompt}\n"
        f"Target area: {area or 'infer from the brief'}\n"
        f"Suggest about {count} places.\n"
        f"Places already in my library (do not repeat these): {existing}"
    )
    raw = _call_anthropic(user) if PROVIDER == "anthropic" else _call_openai(user)
    return _parse(raw)


def _call_openai(user: str) -> str:
    if not API_KEY and not _is_local():
        raise LLMNotConfigured("Set LLM_API_KEY (or point LLM_BASE_URL at a local Ollama).")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    url = f"{_base_url()}/chat/completions"
    resp = httpx.post(url, json=body, headers=headers, timeout=60.0)
    if resp.status_code == 400:  # some OpenAI-compatible servers reject response_format
        body.pop("response_format", None)
        resp = httpx.post(url, json=body, headers=headers, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_anthropic(user: str) -> str:
    if not API_KEY:
        raise LLMNotConfigured("Set LLM_API_KEY for Anthropic.")
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": 1500,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    resp = httpx.post(f"{_base_url()}/v1/messages", json=body, headers=headers, timeout=60.0)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def _parse(raw: str) -> list[ProposedPOI]:
    """Pull the POI list out of the model's reply and validate rows, skipping junk."""
    data = _loads(raw)
    items = data.get("pois") if isinstance(data, dict) else data
    out: list[ProposedPOI] = []
    for it in items or []:
        try:
            out.append(ProposedPOI(**it))
        except Exception:
            continue  # one malformed row shouldn't sink the whole suggestion set
    return out


def _loads(raw: str):
    """json.loads, but tolerant of ```json fences / surrounding prose."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = raw.find(opener), raw.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(raw[i:j + 1])
                except json.JSONDecodeError:
                    pass
        return {}
