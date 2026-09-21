"""Puts the service directory and services/_shared on sys.path for the tests."""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
SHARED_DIR = SERVICE_DIR.parent / "_shared"
for path in (SERVICE_DIR, SHARED_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
