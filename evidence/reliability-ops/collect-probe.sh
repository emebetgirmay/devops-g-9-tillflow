#!/usr/bin/env bash
# Edge probe history (ADR 0010 section 2): what the one-minute probe of the public /ready saw.
#
# devops-g9-probe calls the public API Gateway /ready every minute and writes ProbeSuccess (1/0)
# and ProbeLatencyMs to TillFlow/Probe. CloudWatch keeps one-minute data for 15 days, so this
# script exports it before it rolls up:
#
#   probe-minutes.json         one row per minute: [time, successes, probes, latency_ms]
#   probe-alarm-history.json   probe-down state changes in the window
#   probe-summary.json         availability overall and per UTC day, outages, latency
#
# A minute with no datapoint counts as down (the probe did not run), as probe-down treats it.
# Minutes before the probe's first datapoint are not counted.
#
# Read-only. Needs an SSO session: aws sso login --profile g9
#
#   ./evidence/reliability-ops/collect-probe.sh                        # last 15 days
#   PROBE_FROM=2026-09-28T00:00:00Z ./evidence/reliability-ops/collect-probe.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
PROBE_FROM="${PROBE_FROM:-$(date -u -d '15 days ago' +%FT%H:%M:00Z)}"
PROBE_TO="${PROBE_TO:-$(date -u +%FT%H:%M:00Z)}"

OUT="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "probe window: $PROBE_FROM -> $PROBE_TO"

metric() { # id, metric name, stat
  printf '{"Id":"%s","MetricStat":{"Metric":{"Namespace":"TillFlow/Probe","MetricName":"%s","Dimensions":[{"Name":"Target","Value":"edge-ready"}]},"Period":60,"Stat":"%s"}}' "$1" "$2" "$3"
}
echo "[$(metric ok ProbeSuccess Sum),$(metric n ProbeSuccess SampleCount),$(metric lat ProbeLatencyMs Average)]" >"$TMP/queries.json"

aws cloudwatch get-metric-data --metric-data-queries "file://$TMP/queries.json" \
  --start-time "$PROBE_FROM" --end-time "$PROBE_TO" --scan-by TimestampAscending \
  --query 'MetricDataResults[].{id:Id,t:Timestamps,v:Values}' --output json >"$TMP/data.json"

aws cloudwatch describe-alarm-history --alarm-name devops-g9-probe-down --history-item-type StateUpdate \
  --start-date "$PROBE_FROM" --end-date "$PROBE_TO" \
  --query 'reverse(AlarmHistoryItems)[].{time:Timestamp,summary:HistorySummary}' \
  --output json >"$OUT/probe-alarm-history.json"

python3 - "$TMP/data.json" "$OUT" "$PROBE_FROM" "$PROBE_TO" <<'PY'
import json, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

data_path, out, start, end = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]

def minute(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).replace(second=0, microsecond=0)

series = {}
for result in json.loads(data_path.read_text()):
    series[result["id"]] = {minute(t): v for t, v in zip(result["t"], result["v"])}

if not series.get("n"):
    sys.exit("no ProbeSuccess datapoints in the window")

first, last = min(series["n"]), minute(end) - timedelta(minutes=1)
rows, day = [], defaultdict(lambda: {"minutes": 0, "up": 0, "failed": 0, "missing": 0})
outages, current = [], None
t = first
while t <= last:
    n = series["n"].get(t, 0)
    ok = series["ok"].get(t, 0)
    lat = series["lat"].get(t)
    up = n > 0 and ok >= n
    d = day[t.date().isoformat()]
    d["minutes"] += 1
    if up:
        d["up"] += 1
    elif n:
        d["failed"] += 1
    else:
        d["missing"] += 1
    rows.append([t.strftime("%FT%H:%MZ"), ok, n, None if lat is None else round(lat, 1)])
    if not up:
        if current is None:
            current = {"start": t, "minutes": 0, "failed": 0, "missing": 0}
        current["minutes"] += 1
        current["failed" if n else "missing"] += 1
    elif current is not None:
        outages.append(current)
        current = None
    t += timedelta(minutes=1)
if current is not None:
    outages.append(current)

def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1) if values else None

latencies = [r[3] for r in rows if r[3] is not None]
total = sum(d["minutes"] for d in day.values())
up = sum(d["up"] for d in day.values())
summary = {
    "window": {"from": first.strftime("%FT%H:%MZ"), "to": last.strftime("%FT%H:%MZ"), "requested_from": start},
    "rule": "one probe a minute of the public /ready; a failed or missing minute counts as down",
    "minutes": total,
    "up_minutes": up,
    "failed_minutes": sum(d["failed"] for d in day.values()),
    "missing_minutes": sum(d["missing"] for d in day.values()),
    "availability_percent": round(100 * up / total, 3),
    "latency_ms": {"p50": pct(latencies, 0.50), "p95": pct(latencies, 0.95), "p99": pct(latencies, 0.99), "max": max(latencies, default=None)},
    "by_day": {k: {**v, "availability_percent": round(100 * v["up"] / v["minutes"], 3)} for k, v in sorted(day.items())},
    # Single-minute blips are listed too; probe-down needs two consecutive minutes.
    "outages": [
        {"start": o["start"].strftime("%FT%H:%MZ"), "minutes": o["minutes"], "failed": o["failed"], "missing": o["missing"],
         "would_page": o["minutes"] >= 2}
        for o in outages
    ],
}

(out / "probe-minutes.json").write_text(json.dumps(rows, separators=(",", ":")) + "\n")
(out / "probe-summary.json").write_text(json.dumps(summary, indent=2) + "\n")

print(f"\n{summary['window']['from']} -> {summary['window']['to']}: {total} minutes, "
      f"availability {summary['availability_percent']}% "
      f"({summary['failed_minutes']} failed, {summary['missing_minutes']} missing)")
print(f"latency p50 {summary['latency_ms']['p50']} ms, p95 {summary['latency_ms']['p95']} ms, max {summary['latency_ms']['max']} ms\n")
print("| Day (UTC) | Minutes | Up | Failed | Missing | Availability |")
print("|---|---|---|---|---|---|")
for k, v in summary["by_day"].items():
    print(f"| {k} | {v['minutes']} | {v['up']} | {v['failed']} | {v['missing']} | {v['availability_percent']}% |")
print(f"\n{len(outages)} down stretches, {sum(o['would_page'] for o in summary['outages'])} of 2+ minutes (would page)")
for o in summary["outages"]:
    print(f"  {o['start']}  {o['minutes']} min  (failed {o['failed']}, missing {o['missing']})")
PY
