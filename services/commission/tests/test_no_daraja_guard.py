"""Guard: Commission never touches Daraja or the M-Pesa adapter (ADR 0004).

The worker may use urllib.request, because its one job is to call the Payments API. It may not
import the mpesa package or anything under services/_shared, hold a Safaricom URL, or carry a
secret. This file is excluded from the content scan because it holds the patterns.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

COMMISSION_DIR = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

BANNED = {
    "mpesa",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "http.client",
    "socket",
    "boto3",
    "botocore",
}
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
# Code reaching into services/_shared (the mpesa package) is banned; a doc file *linking* to
# services/_shared/pos-commission-contract.md or pos-payments-contract.md in prose is fine and
# expected, so this one only applies to .py files, not the generic PATTERNS list above.
CODE_ONLY_PATTERNS = [re.compile(r"_shared", re.IGNORECASE)]


def worker_files() -> list[Path]:
    return [
        p
        for p in sorted(COMMISSION_DIR.rglob("*"))
        if p.is_file()
        and "tests" not in p.relative_to(COMMISSION_DIR).parts
        and "__pycache__" not in p.parts
        and p.suffix in {".py", ".md", ".txt", ".sh", ""}
    ]


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
        found += [n for n in names if any(n == b or n.startswith(b + ".") for b in BANNED)]
    return found


class CommissionGuardTest(unittest.TestCase):
    def test_worker_is_found(self) -> None:
        self.assertIn("worker.py", {p.name for p in worker_files()})

    def test_no_adapter_or_daraja_capable_imports(self) -> None:
        for path in worker_files():
            if path.suffix == ".py":
                with self.subTest(file=path.name):
                    self.assertEqual(banned_imports_in(path.read_text()), [])

    def test_no_safaricom_urls_or_secrets(self) -> None:
        for path in worker_files():
            with self.subTest(file=path.name):
                text = path.read_text(errors="replace")
                self.assertEqual([p.pattern for p in PATTERNS if p.search(text)], [])

    def test_no_code_reaches_into_shared(self) -> None:
        for path in worker_files():
            if path.suffix != ".py":
                continue
            with self.subTest(file=path.name):
                text = path.read_text(errors="replace")
                self.assertEqual([p.pattern for p in CODE_ONLY_PATTERNS if p.search(text)], [])

    def test_the_guard_catches_violations(self) -> None:
        self.assertTrue(banned_imports_in("import mpesa"))
        self.assertTrue(banned_imports_in("from mpesa.fake_adapter import FakeAdapter"))
        self.assertTrue(banned_imports_in("import requests"))
        self.assertEqual(banned_imports_in("import urllib.request\nimport json"), [])
        self.assertTrue(
            any(p.search("sys.path.insert(0, '../_" + "shared')") for p in CODE_ONLY_PATTERNS)
        )
        self.assertEqual(
            [
                p.pattern
                for p in PATTERNS
                if p.search("see services/_shared/pos-commission-contract.md")
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
