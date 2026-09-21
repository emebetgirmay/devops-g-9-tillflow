"""Guard: the Payments service has no outbound HTTP client and no Safaricom URL or secret.

ADR 0004: only a deployed sandbox adapter may talk to Daraja, and this build ships none. The
content scan skips this file and the tests, which hold the patterns. reconcile.py may call the
operator-supplied service URL, so it is exempt from the import ban only.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

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
IMPORT_BAN_EXEMPT = {"reconcile.py"}

PATTERNS = [
    re.compile(r"https?://[^\s\"']*(safaricom|daraja)", re.IGNORECASE),
    re.compile(r"safaricom\.co\.ke", re.IGNORECASE),
    re.compile(
        r"(consumer_key|consumer_secret|pass_?key|initiator_password|security_credential)"
        r"\s*[:=]\s*[\"'][^\"']{8,}[\"']",
        re.IGNORECASE,
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".sh", ""}


def service_files() -> list[Path]:
    files = []
    for path in sorted(SERVICE_DIR.rglob("*")):
        parts = set(path.relative_to(SERVICE_DIR).parts)
        if not path.is_file() or parts & {"__pycache__", "data", "tests"}:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name == "Dockerfile":
            files.append(path)
    return files


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
        found += [n for n in names if any(n == b or n.startswith(b + ".") for b in BANNED_IMPORTS)]
    return found


class NoOutboundGuardTest(unittest.TestCase):
    def test_files_are_found(self) -> None:
        names = {p.name for p in service_files()}
        self.assertTrue({"app.py", "payments.py", "payouts.py", "store.py"} <= names)

    def test_no_outbound_http_or_cloud_client_imports(self) -> None:
        for path in service_files():
            if path.suffix != ".py" or path.name in IMPORT_BAN_EXEMPT:
                continue
            with self.subTest(file=str(path.relative_to(SERVICE_DIR))):
                self.assertEqual(banned_imports_in(path.read_text()), [])

    def test_no_safaricom_urls_or_secrets(self) -> None:
        for path in service_files():
            if path.resolve() == THIS_FILE:
                continue
            text = path.read_text(errors="replace")
            hits = [p.pattern for p in PATTERNS if p.search(text)]
            with self.subTest(file=str(path.relative_to(SERVICE_DIR))):
                self.assertEqual(hits, [])

    def test_the_guard_catches_violations(self) -> None:
        self.assertTrue(banned_imports_in("import requests"))
        self.assertTrue(banned_imports_in("from urllib.request import urlopen"))
        self.assertEqual(banned_imports_in("import json\nfrom http.server import HTTPServer"), [])
        host = "sandbox." + "safaricom" + ".co.ke"
        self.assertTrue(any(p.search(f"https://{host}/x") for p in PATTERNS))
        self.assertTrue(any(p.search("consumer_" + "key = '" + "x" * 12 + "'") for p in PATTERNS))


if __name__ == "__main__":
    unittest.main()
