"""Startup refusals: this build never reaches Safaricom and never falls back silently."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from core.config import ConfigError, Settings

SANDBOX_VARS = {
    "MPESA_CONSUMER_KEY": "x",
    "MPESA_CONSUMER_SECRET": "x",
    "MPESA_SHORTCODE": "x",
    "MPESA_PASSKEY": "x",
    "MPESA_CALLBACK_BASE_URL": "https://example.invalid",
}


class SettingsTest(unittest.TestCase):
    def test_defaults_use_the_fake_adapter(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual(settings.adapter, "fake")
        self.assertTrue(settings.confirm_success_with_query)
        self.assertEqual(settings.reconcile_sla_seconds, 120)

    def test_daraja_sandbox_is_refused_without_credentials(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"MPESA_ADAPTER": "daraja_sandbox"})
        self.assertIn("not supported", str(ctx.exception))

    def test_daraja_sandbox_is_refused_even_with_all_credentials(self) -> None:
        env = {"MPESA_ADAPTER": "daraja_sandbox", **SANDBOX_VARS}
        with self.assertRaises(ConfigError):
            Settings.from_env(env)

    def test_unknown_adapter_is_refused_not_replaced(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"MPESA_ADAPTER": "something-else"})

    def test_postgres_is_refused_until_rds_lands(self) -> None:
        for url in ("postgres://u@h/db", "postgresql://u@h/db"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                Settings.from_env({"DATABASE_URL": url})

    def test_database_url_forms(self) -> None:
        self.assertEqual(
            Settings.from_env({"DATABASE_URL": "sqlite:////tmp/x.db"}).db_path, "/tmp/x.db"
        )
        for url in ("sqlite:///:memory:", "sqlite:///", "mysql://x", "/tmp/x.db"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                Settings.from_env({"DATABASE_URL": url})

    def test_bad_numbers_and_flags(self) -> None:
        for env in (
            {"PORT": "abc"},
            {"RECONCILE_SLA_SECONDS": "-1"},
            {"CONFIRM_SUCCESS_WITH_QUERY": "maybe"},
            {"FAKE_CLOCK": "x"},
        ):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                Settings.from_env(env)


if __name__ == "__main__":
    unittest.main()
