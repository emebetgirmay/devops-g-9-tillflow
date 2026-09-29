"""Edge uptime probe (ADR 0010 section 2). EventBridge Scheduler runs it every minute.

Calls the public API Gateway `/ready` (edge -> VPC link -> ALB -> POS) and writes one datapoint
of `ProbeSuccess` (1 or 0) and `ProbeLatencyMs` to namespace `TillFlow/Probe`, dimension
`Target`. A failed probe writes 0 rather than raising, so the uptime panel and `probe-down` see
the failure; a probe that does not run at all leaves missing data, which `probe-down` also
treats as down.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

NAMESPACE = "TillFlow/Probe"

_cloudwatch = None


def _cloudwatch_client():
    global _cloudwatch
    if _cloudwatch is None:
        import boto3  # in the Lambda runtime; imported lazily so tests need no AWS SDK

        _cloudwatch = boto3.client("cloudwatch")
    return _cloudwatch


def check(url: str, timeout: float = 5.0) -> tuple[bool, float, str]:
    """(ok, latency_ms, detail). ok only for HTTP 200 with a JSON body saying ready."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (our own edge)
            body = resp.read(4096)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return False, (time.monotonic() - started) * 1000, f"http {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, (time.monotonic() - started) * 1000, f"error {type(exc).__name__}"
    latency = (time.monotonic() - started) * 1000
    try:
        ready = json.loads(body).get("status") == "ready"
    except (json.JSONDecodeError, AttributeError):
        ready = False
    return status == 200 and ready, latency, f"http {status}"


def handler(_event, _context, client=None, probe=check):
    target = os.environ.get("PROBE_TARGET", "edge-ready")
    ok, latency_ms, detail = probe(os.environ["PROBE_URL"])
    (client or _cloudwatch_client()).put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {
                "MetricName": "ProbeSuccess",
                "Dimensions": [{"Name": "Target", "Value": target}],
                "Value": 1.0 if ok else 0.0,
                "Unit": "Count",
            },
            {
                "MetricName": "ProbeLatencyMs",
                "Dimensions": [{"Name": "Target", "Value": target}],
                "Value": round(latency_ms, 1),
                "Unit": "Milliseconds",
            },
        ],
    )
    result = {"event": "probe", "target": target, "ok": ok, "latency_ms": round(latency_ms, 1), "detail": detail}
    print(json.dumps(result))
    return result
