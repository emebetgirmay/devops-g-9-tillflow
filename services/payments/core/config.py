"""Service settings, read from the environment. Fails loudly on anything unsupported."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

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


ADAPTERS = ("fake", "daraja_sandbox")


@dataclass(frozen=True)
class DarajaConfig:
    """Daraja sandbox B2C access, from Platform-managed secrets (ADR 0004: devops-g9/daraja).

    B2C only for now: STK collection through the real adapter is not built. The security
    credential is supplied already encrypted (the portal generates it; ADR 0008 says one may be
    reused across requests), so this service needs no RSA code or certificate.
    """

    base_url: str
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    b2c_shortcode: str
    b2c_initiator_name: str
    b2c_security_credential: str = field(repr=False)
    callback_base_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DarajaConfig:
        names = {
            "base_url": "MPESA_BASE_URL",
            "consumer_key": "MPESA_CONSUMER_KEY",
            "consumer_secret": "MPESA_CONSUMER_SECRET",
            "b2c_shortcode": "MPESA_B2C_SHORTCODE",
            "b2c_initiator_name": "MPESA_B2C_INITIATOR_NAME",
            "b2c_security_credential": "MPESA_B2C_SECURITY_CREDENTIAL",
            "callback_base_url": "MPESA_CALLBACK_BASE_URL",
        }
        values = {attr: env.get(var, "").strip() for attr, var in names.items()}
        missing = [names[attr] for attr, value in values.items() if not value]
        if missing:
            raise ConfigError(f"MPESA_ADAPTER=daraja_sandbox needs {', '.join(missing)}")
        base = urlparse(values["base_url"])
        # Sandbox only (README conventions): refuse any provider host that is not a sandbox one.
        if base.scheme != "https" or not (base.hostname or "").startswith("sandbox."):
            raise ConfigError("MPESA_BASE_URL must be an https sandbox host (sandbox.<provider>)")
        if urlparse(values["callback_base_url"]).scheme != "https":
            raise ConfigError("MPESA_CALLBACK_BASE_URL must be https")
        values["base_url"] = values["base_url"].rstrip("/")
        values["callback_base_url"] = values["callback_base_url"].rstrip("/")
        return cls(**values)


@dataclass(frozen=True)
class Settings:
    port: int = 8080
    commit_sha: str = "local"
    image_digest: str = "unknown"
    db_path: str = str(SERVICE_DIR / "data" / "payments.db")
    # A postgresql:// URL when deployed on RDS (ADR 0002); empty means the SQLite file above.
    database_url: str = field(default="", repr=False)
    db_pool_size: int = 5
    adapter: str = "fake"
    fake_clock: str = "manual"
    callback_allowed_ips: tuple[str, ...] = ("127.0.0.1", "::1")
    # A header our own edge overwrites with the caller's address (API Gateway parameter mapping).
    # Empty: the allowlist checks the socket peer.
    callback_source_header: str = ""
    confirm_success_with_query: bool = True
    reconcile_sla_seconds: int = 120
    callback_deadline_seconds: int = 90
    created_sweep_seconds: int = 120
    reconcile_window_seconds: int = 86_400
    idempotency_ttl_seconds: int = 7 * 86_400
    payout_max_minor: int = 25_000_000
    payouts_enabled: bool = True
    daraja: DarajaConfig | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        adapter = env.get("MPESA_ADAPTER", "fake").strip() or "fake"
        if adapter not in ADAPTERS:
            # Never fall back to the fake silently.
            raise ConfigError(
                f"MPESA_ADAPTER={adapter!r} is not supported: use one of {', '.join(ADAPTERS)}"
            )
        # ADR 0004: the sandbox adapter belongs to the deployed Payments environment only, with
        # Platform-managed secrets. Missing or non-sandbox settings refuse to start.
        daraja = DarajaConfig.from_env(env) if adapter == "daraja_sandbox" else None
        fake_clock = env.get("FAKE_CLOCK", "manual").strip() or "manual"
        if fake_clock not in ("manual", "system"):
            raise ConfigError("FAKE_CLOCK must be 'manual' or 'system'")
        if daraja is not None:
            fake_clock = "system"  # a real provider runs on real time

        url = env.get("DATABASE_URL", "").strip()
        db_path, database_url = cls.db_path, ""
        if not url:
            pass
        elif url.startswith("sqlite:///"):
            db_path = url[len("sqlite:///") :]
            if not db_path or db_path == ":memory:":
                raise ConfigError("DATABASE_URL must point at a sqlite file")
        elif url.startswith(("postgres://", "postgresql://")):
            database_url = url  # the devops-g9/db/payments secret on RDS (ADR 0002)
        else:
            raise ConfigError("DATABASE_URL must be sqlite:///<path> or postgresql://...")

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
            database_url=database_url,
            db_pool_size=_int(env, "DB_POOL_SIZE", 5) or 1,
            adapter=adapter,
            fake_clock=fake_clock,
            callback_allowed_ips=ips,
            callback_source_header=env.get("CALLBACK_SOURCE_HEADER", "").strip().lower(),
            confirm_success_with_query=_bool(env, "CONFIRM_SUCCESS_WITH_QUERY", True),
            reconcile_sla_seconds=_int(env, "RECONCILE_SLA_SECONDS", 120),
            callback_deadline_seconds=_int(env, "CALLBACK_DEADLINE_SECONDS", 90),
            created_sweep_seconds=_int(env, "CREATED_SWEEP_SECONDS", 120),
            reconcile_window_seconds=_int(env, "RECONCILE_WINDOW_SECONDS", 86_400),
            payout_max_minor=_int(env, "PAYOUT_MAX_MINOR", 25_000_000),
            payouts_enabled=_bool(env, "PAYOUTS_ENABLED", True),
            daraja=daraja,
        )
