#!/usr/bin/env bash
# Run the app against an onboarded city (from data/cities.json). Generalizes
# serve-boston.sh: it just points the same app at a different pre-built engine.
#   python scripts/onboard_city.py <city>   # build/serve the city's Valhalla first
#   bash scripts/serve-city.sh <city>        # -> http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
CITY="${1:?usage: serve-city.sh <city>   (see data/cities.json)}"

set -a; [ -f .env ] && . ./.env; set +a   # keys/overrides first
eval "$(.venv/bin/python - "$CITY" <<'PY'
import json, os, sys
city = sys.argv[1]
c = json.load(open("data/cities.json"))[city]
print(f'export VALHALLA_URL=http://localhost:{c["port"]}')
if c.get("depart"):
    print(f'export VALHALLA_DEPART={c["depart"]}')
pois = f'data/pois.{city}.json'
if os.path.exists(pois):
    print(f'export POIS_PATH={os.path.abspath(pois)}')
b = c["base"]
print(f'export BASE_TIP="{b["name"]} ({b["lat"]} / {b["lon"]})"')
print(f'export BASE_LAT={b["lat"]}')
print(f'export BASE_LON={b["lon"]}')
print(f'export BASE_NAME="{b["name"]}"')
print(f'export CITY_LABEL="{c.get("label", "")}"')
print(f'export DEFAULT_CITY={city}')
PY
)"
source .venv/bin/activate

curl -sf -m 3 "$VALHALLA_URL/status" >/dev/null 2>&1 \
  || echo "⚠  $CITY engine not reachable at $VALHALLA_URL — run: python scripts/onboard_city.py $CITY"
echo "City: $CITY | engine $VALHALLA_URL | POIs ${POIS_PATH:-<default>} | depart ${VALHALLA_DEPART:-auto}"
echo "Base auto-set to $BASE_TIP (override it in the sidebar if you like). For transit, pick Mode = transit."
exec uvicorn --app-dir backend app.main:app --port 8000 --reload
