"""Service settings, read from the environment. Fails loudly on anything unsupported."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """The environment asks for something this build refuses to do."""


class SystemClock:
    def now(self) -> float:
        return time.time()


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigError(f"{name} must not be negative")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    port: int = 8080
    commit_sha: str = "local"
    image_digest: str = "unknown"
    db_path: str = str(SERVICE_DIR / "data" / "payments.db")
    adapter: str = "fake"
    fake_clock: str = "manual"
    callback_allowed_ips: tuple[str, ...] = ("127.0.0.1", "::1")
    confirm_success_with_query: bool = True
    reconcile_sla_seconds: int = 120
    callback_deadline_seconds: int = 90
    created_sweep_seconds: int = 120
    reconcile_window_seconds: int = 86_400
    idempotency_ttl_seconds: int = 7 * 86_400
    payout_max_minor: int = 25_000_000
    payouts_enabled: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        adapter = env.get("MPESA_ADAPTER", "fake").strip() or "fake"
        if adapter != "fake":
            # ADR 0004: the sandbox adapter belongs to the deployed Payments environment with
            # Platform-managed secrets. This build ships the FakeAdapter only and never falls
            # back to it silently.
            raise ConfigError(
                f"MPESA_ADAPTER={adapter!r} is not supported by this build: only 'fake' is "
                "available (no code here talks to Safaricom)"
            )
        fake_clock = env.get("FAKE_CLOCK", "manual").strip() or "manual"
        if fake_clock not in ("manual", "system"):
            raise ConfigError("FAKE_CLOCK must be 'manual' or 'system'")

        url = env.get("DATABASE_URL", "").strip()
        if not url:
            db_path = cls.db_path
        elif url.startswith("sqlite:///"):
            db_path = url[len("sqlite:///") :]
            if not db_path or db_path == ":memory:":
                raise ConfigError("DATABASE_URL must point at a sqlite file")
        elif url.startswith(("postgres://", "postgresql://")):
            raise ConfigError(
                "Postgres is not supported yet: it lands with RDS (ADR 0002). Use a sqlite:/// URL"
            )
        else:
            raise ConfigError("DATABASE_URL must be sqlite:///<path>")

        ips = tuple(
            part.strip()
            for part in env.get("CALLBACK_ALLOWED_IPS", "127.0.0.1,::1").split(",")
            if part.strip()
        )
        if not ips:
            raise ConfigError("CALLBACK_ALLOWED_IPS must list at least one address")

        return cls(
            port=_int(env, "PORT", 8080),
            commit_sha=env.get("COMMIT_SHA", "local"),
            image_digest=env.get("IMAGE_DIGEST", "unknown"),
            db_path=db_path,
            adapter=adapter,
            fake_clock=fake_clock,
            callback_allowed_ips=ips,
            confirm_success_with_query=_bool(env, "CONFIRM_SUCCESS_WITH_QUERY", True),
            reconcile_sla_seconds=_int(env, "RECONCILE_SLA_SECONDS", 120),
            callback_deadline_seconds=_int(env, "CALLBACK_DEADLINE_SECONDS", 90),
            created_sweep_seconds=_int(env, "CREATED_SWEEP_SECONDS", 120),
            reconcile_window_seconds=_int(env, "RECONCILE_WINDOW_SECONDS", 86_400),
            payout_max_minor=_int(env, "PAYOUT_MAX_MINOR", 25_000_000),
            payouts_enabled=_bool(env, "PAYOUTS_ENABLED", True),
        )
