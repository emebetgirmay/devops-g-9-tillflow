"""Path setup. The worker itself imports none of this; only the tests run the real Payments app."""

from __future__ import annotations

import sys
from pathlib import Path

COMMISSION_DIR = Path(__file__).resolve().parent.parent
SERVICES = COMMISSION_DIR.parent
for path in (COMMISSION_DIR, SERVICES / "payments", SERVICES / "_shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
