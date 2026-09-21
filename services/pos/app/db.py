"""DB session wiring.

Defaults to SQLite under /app/data (writable in the image for uid 10001).
Fargate empty volumes mounted at /tmp are root-owned, so do not put the DB
there when the task runs as non-root. Set DATABASE_URL to a
postgresql+psycopg://... DSN when RDS lands; the schema uses no
SQLite-/Postgres-specific types.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////app/data/pos.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
