"""Prometheus metrics for Payments — ADR 0009 sections 1-2.

Stdlib only, no third-party client library: this service has zero third-party
dependencies by design (tests/test_no_outbound_guard.py enforces it), and a
metrics client is not an exception to that rule. So this module is a small,
hand-rolled Prometheus text-exposition writer — the same output shape the
`prometheus_client` library would give you, just built from a plain dict.

Two different kinds of metric live here, and they work very differently:

1. **Counters and histograms** (http_requests_total, create_results_total,
   state_transitions_total, resolution_seconds, callbacks_total,
   adapter_calls_total, adapter_call_duration_seconds, reconcile_runs_total,
   anomalies_total) accumulate in this process's memory between scrapes.
   They reset to zero on every restart, and two tasks running the same
   service have independent counts — that's normal and expected for a
   counter (Grafana sums them across tasks).

2. **Gauges computed from the database** (records, oldest_age_seconds,
   reconcile_last_success_timestamp_seconds, payouts_enabled) are NOT
   accumulated at all. There is no in-process state for them. Every time
   something scrapes GET /metrics, ``render()`` runs a handful of SQL
   queries against the *current* database and reports what it finds right
   now. That's deliberate (ADR 0009 section 1): it means these four numbers
   are correct immediately after a task restart (nothing to warm up), and
   two tasks reading the same database report the *same* numbers — so
   Grafana must aggregate them with ``max``, never ``sum`` (summing would
   double-count the same row as seen by every task).

Hard rule enforced by review, not code: no tenant, attendant, phone number
or record id is ever a label on anything below. That's what would turn a
metric into an unbounded, ever-growing set of time series (a cardinality
explosion) instead of a small fixed dashboard. Those identifiers belong in
logs, not metrics.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Iterable

# Buckets for *_seconds histograms measuring in-request work (HTTP handling,
# a single adapter call). Prometheus convention: each bucket is a count of
# observations <= that boundary, plus a final +Inf bucket that always equals
# the total count.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)

# ADR 0009 section 2's own bucket list for "creation to terminal state" — much
# longer-running than a single request, so a different bucket ladder.
_RESOLUTION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 900, 3600)

_LOCK = threading.Lock()


def _fmt(value: float) -> str:
    """Prometheus text format wants plain decimals, not Python's repr (e.g.
    no trailing "L", and integers render without ".0" the way real exporters
    do, though either is valid — this just reads cleaner in Grafana/curl)."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_str(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values))
    return "{" + pairs + "}"


class Counter:
    """A named, labelled counter. ``.inc(*label_values)`` must pass exactly
    as many values as ``label_names`` — same order, every time, at every
    call site. There's no enforcement of that beyond code review; get the
    label values right, because Prometheus has no way to tell "the 2nd
    label is wrong" from "a brand new time series."
    """

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        with _LOCK:
            self._values[label_values] = self._values.get(label_values, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with _LOCK:
            items = sorted(self._values.items())
        for label_values, value in items:
            lines.append(f"{self.name}{_label_str(self.label_names, label_values)} {_fmt(value)}")
        return lines

    def reset_for_tests(self) -> None:
        with _LOCK:
            self._values.clear()


class Histogram:
    """A named, labelled histogram with cumulative buckets (the Prometheus
    convention: the count for bucket "le=0.5" already includes everything
    that would also match "le=0.1" — that's why ``observe`` increments
    *every* bucket the value falls at or under, not just the smallest one).
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = _LATENCY_BUCKETS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._buckets = tuple(sorted(buckets)) + (float("inf"),)
        self._bucket_counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._counts: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, *label_values: str) -> None:
        with _LOCK:
            counts = self._bucket_counts.setdefault(label_values, [0] * len(self._buckets))
            for i, bound in enumerate(self._buckets):
                if value <= bound:
                    counts[i] += 1
            self._sums[label_values] = self._sums.get(label_values, 0.0) + value
            self._counts[label_values] = self._counts.get(label_values, 0) + 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with _LOCK:
            items = sorted(self._bucket_counts.items())
            sums = dict(self._sums)
            counts = dict(self._counts)
        for label_values, bucket_counts in items:
            for bound, cumulative in zip(self._buckets, bucket_counts):
                le = "+Inf" if bound == float("inf") else _fmt(bound)
                le_names = (*self.label_names, "le")
                le_values = (*label_values, le)
                lines.append(f"{self.name}_bucket{_label_str(le_names, le_values)} {cumulative}")
            lines.append(
                f"{self.name}_sum{_label_str(self.label_names, label_values)} "
                f"{_fmt(sums[label_values])}"
            )
            lines.append(
                f"{self.name}_count{_label_str(self.label_names, label_values)} "
                f"{counts[label_values]}"
            )
        return lines

    def reset_for_tests(self) -> None:
        with _LOCK:
            self._bucket_counts.clear()
            self._sums.clear()
            self._counts.clear()


class Timer:
    """``with Timer() as t: ...`` then ``t.elapsed`` — used at every adapter
    and HTTP call site instead of hand-rolling perf_counter arithmetic
    everywhere."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.elapsed = time.perf_counter() - self._start


# --- The metric catalogue (ADR 0009 section 2) ------------------------------------------------

http_requests_total = Counter(
    "payments_http_requests_total",
    "HTTP requests handled by Payments, by route template and status class.",
    ("route", "status_class"),
)
http_request_duration_seconds = Histogram(
    "payments_http_request_duration_seconds",
    "HTTP request duration in seconds, by route template.",
    ("route",),
)
create_results_total = Counter(
    "payments_create_results_total",
    "Outcome of POST /payments and POST /payouts.",
    ("operation", "result", "state"),
)
state_transitions_total = Counter(
    "payments_state_transitions_total",
    "Every payment/disbursement state change.",
    ("kind", "from_state", "to_state"),
)
resolution_seconds = Histogram(
    "payments_resolution_seconds",
    "Creation to terminal state, in seconds.",
    ("kind", "outcome"),
    buckets=_RESOLUTION_BUCKETS,
)
callbacks_total = Counter(
    "payments_callbacks_total", "Provider callback handling outcomes.", ("kind", "result")
)
adapter_calls_total = Counter(
    "payments_adapter_calls_total", "Provider health as the adapter sees it.", ("op", "result")
)
adapter_call_duration_seconds = Histogram(
    "payments_adapter_call_duration_seconds", "Provider call latency.", ("op",)
)
reconcile_runs_total = Counter(
    "payments_reconcile_runs_total", "Reconcile pass outcomes.", ("result",)
)
anomalies_total = Counter(
    "payments_anomalies_total",
    "Illegal transitions, amount mismatches, constraint violations and the like.",
    ("kind", "severity"),
)

_COUNTERS: tuple[Counter, ...] = (
    http_requests_total,
    create_results_total,
    state_transitions_total,
    callbacks_total,
    adapter_calls_total,
    reconcile_runs_total,
    anomalies_total,
)
_HISTOGRAMS: tuple[Histogram, ...] = (
    http_request_duration_seconds,
    resolution_seconds,
    adapter_call_duration_seconds,
)


def reset_for_tests() -> None:
    """Every metric above is a module-level global, so counts persist across
    the whole test process unless cleared. tests/helpers.py's ServiceTestCase
    calls this in setUp — see that class for why (the same reason
    services/pos/tests/conftest.py's `_reset_metrics` fixture exists)."""
    for metric in _COUNTERS:
        metric.reset_for_tests()
    for metric in _HISTOGRAMS:
        metric.reset_for_tests()


# --- Gauges computed fresh from the database at scrape time -----------------------------------

_GAUGE_STATES = ("PENDING", "UNKNOWN", "NEEDS_REVIEW")


def _records_rows(conn: sqlite3.Connection) -> Iterable[tuple[tuple[str, str], int]]:
    for kind, table in (("payment", "payments"), ("disbursement", "disbursements")):
        for row in conn.execute(f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state"):
            yield (kind, row["state"]), row["n"]


def _oldest_age_rows(conn: sqlite3.Connection, now: float) -> Iterable[tuple[tuple[str, str], float]]:
    placeholders = ",".join("?" for _ in _GAUGE_STATES)
    for kind, table in (("payment", "payments"), ("disbursement", "disbursements")):
        # updated_at is bumped by every Store.transition() call (the only
        # writer of the state column), so "oldest updated_at for this state"
        # is exactly "how long has the oldest row been sitting in it."
        rows = conn.execute(
            f"SELECT state, MIN(updated_at) AS oldest FROM {table}"
            f" WHERE state IN ({placeholders}) GROUP BY state",
            _GAUGE_STATES,
        )
        for row in rows:
            yield (kind, row["state"]), max(0.0, now - row["oldest"])


def _reconcile_last_success_row(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT updated_at FROM flags WHERE name = 'reconcile_last_success'"
    ).fetchone()
    return None if row is None else row["updated_at"]


def _payouts_enabled_row(conn: sqlite3.Connection, default: bool) -> bool:
    row = conn.execute("SELECT value FROM flags WHERE name = 'payouts_enabled'").fetchone()
    return default if row is None else bool(row["value"])


def render_gauges(conn: sqlite3.Connection, *, now: float, payouts_enabled_default: bool) -> list[str]:
    lines: list[str] = []

    lines += ["# HELP payments_records Rows by state.", "# TYPE payments_records gauge"]
    for (kind, state), count in sorted(_records_rows(conn)):
        lines.append(f'payments_records{{kind="{kind}",state="{state}"}} {count}')

    lines += [
        "# HELP payments_oldest_age_seconds Age of the oldest row in that state.",
        "# TYPE payments_oldest_age_seconds gauge",
    ]
    for (kind, state), age in sorted(_oldest_age_rows(conn, now)):
        lines.append(f'payments_oldest_age_seconds{{kind="{kind}",state="{state}"}} {_fmt(age)}')

    last_success = _reconcile_last_success_row(conn)
    lines += [
        "# HELP payments_reconcile_last_success_timestamp_seconds "
        "Unix time the reconcile pass last completed without error.",
        "# TYPE payments_reconcile_last_success_timestamp_seconds gauge",
    ]
    if last_success is not None:
        lines.append(f"payments_reconcile_last_success_timestamp_seconds {_fmt(last_success)}")

    enabled = _payouts_enabled_row(conn, payouts_enabled_default)
    lines += [
        "# HELP payments_payouts_enabled The payouts kill switch (1 on, 0 tripped).",
        "# TYPE payments_payouts_enabled gauge",
        f"payments_payouts_enabled {1 if enabled else 0}",
    ]
    return lines


def render(conn: sqlite3.Connection, *, now: float, payouts_enabled_default: bool) -> bytes:
    lines: list[str] = []
    for metric in _COUNTERS:
        lines += metric.render()
    for metric in _HISTOGRAMS:
        lines += metric.render()
    lines += render_gauges(conn, now=now, payouts_enabled_default=payouts_enabled_default)
    return ("\n".join(lines) + "\n").encode("utf-8")


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
