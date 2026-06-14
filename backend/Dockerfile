# Trip Planner API (FastAPI + OR-Tools solver) — also serves the static frontend.
# Build context is the repo ROOT (so the image can include frontend/ + data seeds):
#   docker build -f backend/Dockerfile -t trip-planner-api .
# In the stack it's built by docker-compose (service `api`).
FROM python:3.12-slim
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
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "--app-dir", "backend", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
