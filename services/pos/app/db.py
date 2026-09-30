"""DB session wiring.

Default SQLite path is under /tmp so local pytest (and any host without
/app/data) can start. Deployed ECS sets DATABASE_URL=sqlite:////app/data/pos.db
(see Dockerfile + infra) until `pos_database` flips to "rds" (ADR 0002), at
which point ECS injects a postgresql+psycopg:// DSN from the pos secret instead
-- this module doesn't care which, it dispatches purely on the URL scheme.

On RDS, `pool_pre_ping` matters in a way it never did for a same-process
SQLite file: a connection can go stale (an idle timeout, a failover, the
`docker rds reboot-db-instance` G4 drill) without this process ever knowing,
and a stale connection would otherwise surface as a random mid-request
error instead of SQLAlchemy quietly reconnecting before handing it out. The
pool is deliberately small -- this is one task talking to one instance, not
a fan-out worker pool.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////tmp/pos.db")
_is_sqlite = DATABASE_URL.startswith("sqlite")

_connect_args = {"check_same_thread": False} if _is_sqlite else {}
_engine_kwargs = {} if _is_sqlite else {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True, **_engine_kwargs)
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


def is_database_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — /ready must report false, never raise
        return False
