"""Guard: nothing in services/_shared may reach Daraja or carry Safaricom URLs or secrets.

ADR 0004: only the deployed Payments sandbox adapter talks to Daraja. CI and k6 use the
FakeAdapter. This file is excluded from the content scan because it holds the patterns.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections.abc import Iterable
from pathlib import Path

SHARED_ROOT = Path(__file__).resolve().parent.parent
MPESA_PKG = SHARED_ROOT / "mpesa"
THIS_FILE = Path(__file__).resolve()

# Any network or cloud-secret client inside the M-Pesa package is a boundary violation: the
# fake needs none, and a real adapter does not belong in _shared.
BANNED_IMPORTS = {
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "http.client",
    "urllib.request",
    "socket",
    "ssl",
    "boto3",
    "botocore",
}

URL_PATTERNS = [
    re.compile(r"https?://[^\s\"']*(safaricom|daraja)", re.IGNORECASE),
    re.compile(r"safaricom\.co\.ke", re.IGNORECASE),
]

SECRET_PATTERNS = [
    re.compile(
        r"(consumer_key|consumer_secret|pass_?key|initiator_password|security_credential)"
        r"\s*[:=]\s*[\"'][^\"']{8,}[\"']",
        re.IGNORECASE,
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
]

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh"}


def scannable_files() -> Iterable[Path]:
    for path in sorted(SHARED_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.resolve() == THIS_FILE or path.suffix not in TEXT_SUFFIXES:
            continue
        yield path


def find_hits(text: str) -> list[str]:
    return [p.pattern for p in URL_PATTERNS + SECRET_PATTERNS if p.search(text)]


def banned_imports_in(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] + [f"{module}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if any(name == b or name.startswith(b + ".") for b in BANNED_IMPORTS):
                found.append(name)
    return found


class NoDarajaGuardTest(unittest.TestCase):
    def test_scanner_finds_files(self) -> None:
        self.assertGreater(len(list(scannable_files())), 0)

    def test_no_http_or_cloud_client_imports_in_mpesa_package(self) -> None:
        for path in MPESA_PKG.rglob("*.py"):
            with self.subTest(file=path.name):
                self.assertEqual(banned_imports_in(path.read_text()), [])

    def test_no_safaricom_urls_or_secrets_anywhere_in_shared(self) -> None:
        for path in scannable_files():
            with self.subTest(file=str(path.relative_to(SHARED_ROOT))):
                self.assertEqual(find_hits(path.read_text()), [])


class GuardSelfTest(unittest.TestCase):
    """The guard must actually catch violations, so build bad samples at runtime."""

    def test_detects_banned_imports(self) -> None:
        for src in (
            "import requests",
            "import urllib.request",
            "from urllib import request\nfrom urllib.request import urlopen",
            "from http.client import HTTPSConnection",
            "import boto3",
        ):
            with self.subTest(src=src):
                self.assertTrue(banned_imports_in(src))
        self.assertEqual(banned_imports_in("import json\nfrom decimal import Decimal"), [])

    def test_detects_urls_and_secrets(self) -> None:
        host = "sandbox." + "safaricom" + ".co.ke"
        bad_samples = [
            f"BASE = 'https://{host}/oauth/v1/generate'",
            "consumer_" + "key = '" + "x" * 16 + "'",
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "-----BEGIN " + "RSA PRIVATE KEY-----",
            "ghp_" + "a" * 36,
        ]
        for sample in bad_samples:
            with self.subTest(sample=sample[:20]):
                self.assertTrue(find_hits(sample))

    def test_clean_text_passes(self) -> None:
        self.assertEqual(find_hits("The fake never calls Daraja. consumer_key comes from ops."), [])


if __name__ == "__main__":
    unittest.main()
