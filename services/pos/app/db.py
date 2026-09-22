"""DB session wiring.

Default SQLite path is under /tmp so local pytest (and any host without
/app/data) can start. Deployed ECS sets DATABASE_URL=sqlite:////app/data/pos.db
(see Dockerfile + infra) because the Fargate /tmp volume is root-owned.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////tmp/pos.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _ensure_sqlite_parent(url: str) -> None:
    if not url.startswith("sqlite:///"):
        return
    path = url.removeprefix("sqlite:///")
    if path in ("", ":memory:") or path.startswith(":"):
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    _ensure_sqlite_parent(DATABASE_URL)
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
