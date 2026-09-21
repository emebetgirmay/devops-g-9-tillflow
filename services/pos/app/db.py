"""DB session wiring.

Defaults to a SQLite file under /tmp (the one writable path in the
read-only-root-filesystem ECS task — see infra/envs/sandbox/ecs.tf) so the
service runs standalone before Platform wires up RDS. Set DATABASE_URL to a
postgresql+psycopg://... DSN in deployed envs; the schema (app/models.py)
uses no SQLite- or Postgres-specific column types, so no code change is
needed to switch.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////tmp/pos.db")

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
