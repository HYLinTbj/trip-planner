# Trip Planner API (FastAPI + OR-Tools solver) — also serves the static frontend.
# Build context is the repo ROOT (so the image can include frontend/ + data seeds):
#   docker build -f backend/Dockerfile -t trip-planner-api .   # runtime image (default)
#   docker build -f backend/Dockerfile --target test .         # run the unit-test gate
# In the stack it's built by docker-compose (service `api`; default target = runtime).
FROM python:3.12-slim AS base
WORKDIR /app

# System deps: OR-Tools wheels are self-contained; we just need a C runtime + curl
# for the healthcheck. Keep the layer small.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first so the layer caches across code changes.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code, static frontend, DB migrations, and the data the api reads at runtime
# (regions registry for the routing table; cities.json is only a seed but small).
COPY backend/ backend/
COPY frontend/ frontend/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini
COPY data/regions.json data/regions.json
COPY data/cities.json data/cities.json
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1

# --- test stage: opt-in build-time gate (`docker build --target test .`). It's not in
# the runtime stage's dependency chain, so `docker compose build api` never runs it. ---
FROM base AS test
COPY backend/requirements-dev.txt backend/requirements-dev.txt
RUN pip install --no-cache-dir -r backend/requirements-dev.txt
COPY pyproject.toml pyproject.toml
RUN pytest

# --- runtime stage: the DEFAULT build target (last stage), kept lean + test-free. ---
FROM base AS runtime
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "--app-dir", "backend", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
