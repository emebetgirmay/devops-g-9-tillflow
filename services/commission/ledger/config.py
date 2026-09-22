"""Commission settings, read from the environment. Fails loudly on anything unsupported.

Mirrors services/payments/core/config.py's shape (sqlite-only for now, explicit refusal rather
than a silent fallback) so the two services stay easy to operate the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent

# ADR 0008 section 6, documented Daraja B2C limits (verified against the portal, 2026-09-21):
# minimum KSh 10, maximum KSh 250,000 per transaction. Amounts are minor units.
MIN_PAYOUT_MINOR = 1_000
PROVIDER_MAX_PAYOUT_MINOR = 25_000_000

# ADR 0008 open question 4: business-day cutoff is undecided. Default is a plain EAT (UTC+3)
# calendar day with no special cutoff hour — the conservative placeholder until Product decides
# otherwise. A sale is attributed to the EAT calendar date its PAID transition happened on.
DEFAULT_BUSINESS_DAY_UTC_OFFSET_HOURS = 3


class ConfigError(Exception):
    """The environment asks for something this build refuses to do."""


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


@dataclass(frozen=True)
class Settings:
    db_path: str = str(SERVICE_DIR / "data" / "commission.db")
    pos_base_url: str = "http://pos.internal:8080"
    payments_base_url: str = "http://payments.internal:8080"
    payout_max_minor: int = PROVIDER_MAX_PAYOUT_MINOR
    payout_min_minor: int = MIN_PAYOUT_MINOR
    business_day_utc_offset_hours: int = DEFAULT_BUSINESS_DAY_UTC_OFFSET_HOURS
    close_page_limit: int = 500
    http_timeout_seconds: float = 30.0
    reconcile_sla_seconds: int = 120

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        import os

        env = os.environ if env is None else env

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

        payout_max = _int(env, "PAYOUT_MAX_MINOR", PROVIDER_MAX_PAYOUT_MINOR)
        if payout_max > PROVIDER_MAX_PAYOUT_MINOR:
            raise ConfigError(
                f"PAYOUT_MAX_MINOR must not exceed the provider maximum {PROVIDER_MAX_PAYOUT_MINOR}"
            )
        payout_min = _int(env, "PAYOUT_MIN_MINOR", MIN_PAYOUT_MINOR)
        if payout_min < MIN_PAYOUT_MINOR:
            raise ConfigError(
                f"PAYOUT_MIN_MINOR must not be below the provider minimum {MIN_PAYOUT_MINOR}"
            )

        return cls(
            db_path=db_path,
            pos_base_url=env.get("POS_BASE_URL", "http://pos.internal:8080").rstrip("/"),
            payments_base_url=env.get(
                "PAYMENTS_BASE_URL", env.get("PAYMENTS_URL", "http://payments.internal:8080")
            ).rstrip("/"),
            payout_max_minor=payout_max,
            payout_min_minor=payout_min,
            business_day_utc_offset_hours=_int(
                env, "BUSINESS_DAY_UTC_OFFSET_HOURS", DEFAULT_BUSINESS_DAY_UTC_OFFSET_HOURS
            ),
            close_page_limit=_int(env, "CLOSE_PAGE_LIMIT", 500),
            http_timeout_seconds=float(env.get("HTTP_TIMEOUT_SECONDS", "30") or "30"),
            reconcile_sla_seconds=_int(env, "RECONCILE_SLA_SECONDS", 120),
        )
