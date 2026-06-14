"""Database engine + session (scale-up phase 1).

The single place that knows how to connect. `DATABASE_URL` points at the Postgres
`db` compose service — `localhost` in dev, host `db` once the api is containerized
(phase 4). Follows the codebase's os.environ config convention (cf. VALHALLA_URL).
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://tripplanner:tripplanner@localhost:5432/tripplanner",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model and Alembic's metadata target."""


def get_session() -> Session:
    """FastAPI dependency: yield a session, always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
