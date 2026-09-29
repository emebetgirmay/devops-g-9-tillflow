#!/usr/bin/env python3
"""Generate the TillFlow Grafana dashboards (ADR 0010 section 1). Standard library only.

    python3 infra/grafana/build.py        # rewrites infra/grafana/*.json

The JSON files are the contract: import them into Grafana Cloud (Dashboards -> New -> Import).
Edit this script, not the JSON, then re-import; a change made only in the Grafana UI does not count.

Conventions:
- Every panel uses the `datasource` variable (a CloudWatch data source), so the files import into
  any stack.
- ALB and ECS dimensions come from dashboard variables that look the values up by name, so no
  AWS resource ID is hard-coded.
- SLO stats use one CloudWatch period equal to the panel's window (5 m, 1 h, 28 d), so each value
  is a ratio of sums, not an average of ratios. A missing 5xx series is filled with 0.
- App metrics arrive through ADOT's EMF exporter with an extra `OTelLib` dimension, so they are
  queried with SEARCH() over the full dimension set rather than an exact match (ADR 0010).
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGION = "eu-north-1"
DS = {"type": "cloudwatch", "uid": "${datasource}"}
WINDOWS = (("5m", 300), ("1h", 3600), ("28d", 2419200))

SERVICES = {
    "pos": {"title": "POS", "slo": 0.999, "tg_var": "pos_tg", "ecs": "devops-g9-pos"},
    "payments": {"title": "Payments", "slo": 0.995, "tg_var": "payments_tg", "ecs": "devops-g9-payments"},
}


# --- query builders ---------------------------------------------------------------------------


def metric(ref, namespace, name, dims, stat, period="", hide=False, label=""):
    return {
        "refId": ref,
        "id": ref,
        "datasource": DS,
        "queryMode": "Metrics",
        "metricQueryType": 0,
        "metricEditorMode": 0,
        "region": REGION,
        "namespace": namespace,
        "metricName": name,
        "dimensions": dims,
        "matchExact": True,
        "statistic": stat,
        "period": str(period) if period else "",
        "expression": "",
        "label": label,
        "hide": hide,
    }


def expr(ref, expression, period="", label="", hide=False):
    return {
        "refId": ref,
        "id": ref,
        "datasource": DS,
        "queryMode": "Metrics",
        "metricQueryType": 0,
        "metricEditorMode": 1,
        "region": REGION,
        "expression": expression,
        "period": str(period) if period else "",
        "label": label,
        "hide": hide,
    }


def search(ref, dims, name, stat="Sum", label="", extra=""):
    """SEARCH over an app metric's full dimension set (TillFlow namespace, plus OTelLib)."""
    schema = ",".join(["TillFlow", "OTelLib", *dims])
    term = f'MetricName="{name}"' + (f" {extra}" if extra else "")
    return expr(ref, f"SEARCH('{{{schema}}} {term}', '{stat}', 300)", label=label)


def alb(svc, ref, name, stat, period="", hide=False, label=""):
    dims = {"LoadBalancer": "$lb", "TargetGroup": f"${SERVICES[svc]['tg_var']}"}
    return metric(ref, "AWS/ApplicationELB", name, dims, stat, period, hide, label)


def ecs(svc, ref, name, label=""):
    dims = {"ClusterName": "devops-g9", "ServiceName": SERVICES[svc]["ecs"]}
    return metric(ref, "AWS/ECS", name, dims, "Average", label=label)


# --- panel builders ---------------------------------------------------------------------------


class Layout:
    def __init__(self):
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.next_id = 1

    def place(self, w, h):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        pid = self.next_id
        self.next_id += 1
        return pid, pos

    def row(self, title):
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pid, pos = self.place(24, 1)
        self.x, self.y, self.row_h = 0, self.y + 1, 0
        return {"id": pid, "type": "row", "title": title, "gridPos": pos, "collapsed": False, "panels": []}


def stat(lay, title, targets, unit="percentunit", thresholds=None, decimals=3, w=4, h=4, desc="", time_from=None):
    pid, pos = lay.place(w, h)
    steps = thresholds or [{"color": "green", "value": None}]
    panel = {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": pos,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"},
                "noValue": "no traffic",
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
        },
    }
    if time_from:
        panel["timeFrom"] = time_from
        panel["hideTimeOverride"] = True
    return panel


def series(
    lay, title, targets, unit="short", w=12, h=8, desc="", lines=None, stack=False,
    no_value="no data yet", soft_max=None, overrides=None,
):
    pid, pos = lay.place(w, h)
    custom = {"lineWidth": 1, "fillOpacity": 10, "spanNulls": True, "showPoints": "never", "axisSoftMin": 0}
    if soft_max is not None:
        custom["axisSoftMax"] = soft_max
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    defaults = {"unit": unit, "custom": custom, "noValue": no_value}
    if lines:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "transparent", "value": None}]
            + [{"color": c, "value": v} for v, c in lines],
        }
        # Dashed lines only: shaded areas stretch the y-axis and hide the series.
        custom["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": pos,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }


def slo_thresholds(target):
    return [
        {"color": "red", "value": None},
        {"color": "orange", "value": target - (1 - target)},
        {"color": "green", "value": target},
    ]


BUDGET_THRESHOLDS = [
    {"color": "red", "value": None},
    {"color": "orange", "value": 0.25},
    {"color": "green", "value": 0.5},
]


def availability_targets(svc, period):
    """ALB availability: 1 - 5xx / requests over exactly one period."""
    return [
        alb(svc, "e", "HTTPCode_Target_5XX_Count", "Sum", period, hide=True),
        alb(svc, "r", "RequestCount", "Sum", period, hide=True),
        expr("sli", "IF(r > 0, 1 - FILL(e, 0) / r, 1)", period, label="availability"),
    ]


def budget_targets(svc, period):
    slack = round(1 - SERVICES[svc]["slo"], 6)
    return [
        alb(svc, "e", "HTTPCode_Target_5XX_Count", "Sum", period, hide=True),
        alb(svc, "r", "RequestCount", "Sum", period, hide=True),
        expr("budget", f"IF(r > 0, 1 - (FILL(e, 0) / r) / {slack}, 1)", period, label="budget remaining"),
    ]


def burn_targets(svc):
    slack = round(1 - SERVICES[svc]["slo"], 6)
    return [
        alb(svc, "e", "HTTPCode_Target_5XX_Count", "Sum", 300, hide=True),
        alb(svc, "r", "RequestCount", "Sum", 300, hide=True),
        expr("burn", f"IF(r > 0, (FILL(e, 0) / r) / {slack}, 0)", 300, label="burn rate (5 min)"),
    ]


# --- dashboard scaffolding --------------------------------------------------------------------


def variable_dimension(name, label, dim_key, regex):
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DS,
        "query": {
            "queryType": "dimensionValues",
            "region": REGION,
            "namespace": "AWS/ApplicationELB",
            "metricName": "RequestCount",
            "dimensionKey": dim_key,
            "refId": f"var-{name}",
        },
        "definition": f"dimension_values({REGION},AWS/ApplicationELB,RequestCount,{dim_key})",
        "regex": regex,
        "refresh": 1,
        "hide": 2,
        "sort": 1,
    }


def dashboard(uid, title, description, panels, links=True):
    templating = [
        {
            "name": "datasource",
            "label": "CloudWatch",
            "type": "datasource",
            "query": "cloudwatch",
            "refresh": 1,
            "hide": 0,
        },
        variable_dimension("lb", "Load balancer", "LoadBalancer", "/devops-g9-alb/"),
        variable_dimension("pos_tg", "POS target group", "TargetGroup", "/devops-g9-pos//"),
        variable_dimension("payments_tg", "Payments target group", "TargetGroup", "/devops-g9-payments//"),
    ]
    board = {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["tillflow", "devops-g9", "g3"],
        "timezone": "utc",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": 1,
        "templating": {"list": templating},
        "panels": panels,
        "annotations": {"list": []},
    }
    if links:
        board["links"] = [
            {"title": "TillFlow", "type": "dashboards", "tags": ["tillflow"], "asDropdown": True},
            {
                "title": "Runbook",
                "type": "link",
                "url": "https://github.com/emebetgirmay/devops-g-9-tillflow/blob/main/docs/runbook.md",
                "targetBlank": True,
            },
        ]
    return board


# --- the dashboards ---------------------------------------------------------------------------


def overview():
    lay = Layout()
    p = [lay.row("Uptime: public edge probe (API Gateway -> ALB -> POS /ready, every minute)")]
    for label, secs in WINDOWS:
        p.append(
            stat(
                lay,
                f"Edge uptime {label}",
                [metric("u", "TillFlow/Probe", "ProbeSuccess", {"Target": "edge-ready"}, "Average", secs)],
                thresholds=slo_thresholds(0.999),
                w=4,
                time_from=label,
                desc="Share of one-minute probes that got /ready = ready. No datapoint counts as down in the alarm.",
            )
        )
    p.append(
        series(
            lay,
            "Probe result and latency",
            [
                metric("s", "TillFlow/Probe", "ProbeSuccess", {"Target": "edge-ready"}, "Minimum", 60, label="success"),
                metric("l", "TillFlow/Probe", "ProbeLatencyMs", {"Target": "edge-ready"}, "Average", 60, label="latency ms"),
            ],
            w=12,
            h=4,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "success"},
                    "properties": [
                        {"id": "custom.axisPlacement", "value": "right"},
                        {"id": "min", "value": 0},
                        {"id": "max", "value": 1},
                        {"id": "unit", "value": "none"},
                    ],
                },
                {"matcher": {"id": "byName", "options": "latency ms"}, "properties": [{"id": "unit", "value": "ms"}]},
            ],
        )
    )

    for svc, s in SERVICES.items():
        slack = round(1 - s["slo"], 6)
        p.append(
            lay.row(f"{s['title']} SLO: target {s['slo'] * 100:.1f}%, 28-day error budget {slack * 100:.1f}% (ALB 5xx, interim SLI)")
        )
        for label, secs in WINDOWS:
            p.append(
                stat(
                    lay,
                    f"Availability {label}",
                    availability_targets(svc, secs),
                    thresholds=slo_thresholds(s["slo"]),
                    w=3,
                    time_from=label,
                    desc=f"1 - 5xx / requests at the ALB over the last {label}. Target {s['slo'] * 100:.1f}%.",
                )
            )
        p.append(
            stat(
                lay,
                "Budget left 28d",
                budget_targets(svc, 2419200),
                thresholds=BUDGET_THRESHOLDS,
                decimals=1,
                w=4,
                time_from="28d",
                desc="1 - (errors / requests) / (1 - target). Freeze risky deploys at 0 (SLO doc budget policy).",
            )
        )
        p.append(
            series(
                lay,
                f"{s['title']} burn rate (x budget)",
                burn_targets(svc),
                unit="none",
                w=11,
                h=4,
                lines=[(6, "orange"), (14.4, "red")],
                soft_max=20,
                no_value="0 (no traffic)",
                desc="Orange line: slow-burn alarm (6x over 30 min). Red: fast-burn alarm (14.4x over 5 min).",
            )
        )

    p.append(lay.row("Business signals"))
    p.append(series(lay, "Sales paid (per 5 min)", [search("a", [], "pos_sales_paid_total", label="paid")], w=8))
    p.append(
        series(
            lay,
            "Payments created, by result",
            [
                search(
                    "a",
                    ["operation", "result", "state"],
                    "payments_create_results_total",
                    label="${PROP('Dim.operation')} ${PROP('Dim.result')} ${PROP('Dim.state')}",
                )
            ],
            w=8,
            stack=True,
        )
    )
    p.append(
        series(
            lay,
            "Critical anomalies (any is a P0)",
            [
                search(
                    "a",
                    ["kind", "severity"],
                    "payments_anomalies_total",
                    label="${PROP('Dim.kind')} ${PROP('Dim.severity')}",
                )
            ],
            w=8,
            lines=[(1, "red")],
            no_value="0 (none)",
        )
    )
    return dashboard(
        "tillflow-overview",
        "TillFlow overview",
        "Uptime, SLO, budget remaining and burn rate per service (ADR 0010). Generated by infra/grafana/build.py.",
        p,
    )


def red_and_saturation(lay, svc):
    s = SERVICES[svc]
    p = [lay.row(f"{s['title']} RED at the ALB")]
    p.append(series(lay, "Requests per 5 min", [alb(svc, "r", "RequestCount", "Sum", 300, label="requests")], w=8))
    p.append(
        series(
            lay,
            "Errors per 5 min",
            [
                alb(svc, "e5", "HTTPCode_Target_5XX_Count", "Sum", 300, label="5xx"),
                alb(svc, "e4", "HTTPCode_Target_4XX_Count", "Sum", 300, label="4xx"),
            ],
            w=8,
        )
    )
    p.append(
        series(
            lay,
            "Latency (target response time)",
            [
                alb(svc, "p95", "TargetResponseTime", "p95", 300, label="p95"),
                alb(svc, "p50", "TargetResponseTime", "p50", 300, label="p50"),
            ],
            unit="s",
            w=8,
            lines=[(0.5, "orange")],
            desc="Orange line: the SLO doc's p95 threshold, 500 ms.",
        )
    )
    p.append(lay.row(f"{s['title']} saturation"))
    p.append(
        series(
            lay,
            "CPU and memory (%)",
            [ecs(svc, "cpu", "CPUUtilization", "CPU"), ecs(svc, "mem", "MemoryUtilization", "memory")],
            unit="percent",
            w=12,
            lines=[(70, "orange")],
            desc="Orange line: the ecs-cpu-high alarm and the target-tracking line (70%).",
        )
    )
    p.append(
        series(
            lay,
            "Healthy targets",
            [alb(svc, "h", "HealthyHostCount", "Minimum", 60, label="healthy"), alb(svc, "u", "UnHealthyHostCount", "Maximum", 60, label="unhealthy")],
            w=12,
        )
    )
    return p


def pos():
    lay = Layout()
    p = red_and_saturation(lay, "pos")
    p.append(lay.row("POS app metrics (GET /metrics, scraped by ADOT)"))
    p.append(
        series(
            lay,
            "Requests by route and status",
            [search("a", ["route", "status_class"], "pos_http_requests_total", label="${PROP('Dim.route')} ${PROP('Dim.status_class')}")],
            w=12,
            stack=True,
        )
    )
    p.append(
        series(
            lay,
            "Sale creates by result",
            [search("a", ["result"], "pos_sale_creates_total", label="${PROP('Dim.result')}")],
            w=12,
            stack=True,
            desc="created, replayed (idempotent retry), conflict, invalid.",
        )
    )
    p.append(
        series(
            lay,
            "Payment events applied to sales",
            [search("a", ["result"], "pos_payment_events_total", label="${PROP('Dim.result')}")],
            w=12,
            stack=True,
        )
    )
    p.append(series(lay, "Sales reaching PAID", [search("a", [], "pos_sales_paid_total", label="paid")], w=12))
    return dashboard("tillflow-pos", "TillFlow POS", "POS RED, saturation and business signals. Generated by infra/grafana/build.py.", p)


def payments():
    lay = Layout()
    p = red_and_saturation(lay, "payments")
    p.append(lay.row("Payments app metrics (ADR 0009, GET /metrics scraped by ADOT)"))
    p.append(
        series(
            lay,
            "Creates by operation and result",
            [
                search(
                    "a",
                    ["operation", "result", "state"],
                    "payments_create_results_total",
                    label="${PROP('Dim.operation')} ${PROP('Dim.result')} ${PROP('Dim.state')}",
                )
            ],
            w=12,
            stack=True,
        )
    )
    p.append(
        series(
            lay,
            "Callbacks by kind and result",
            [search("a", ["kind", "result"], "payments_callbacks_total", label="${PROP('Dim.kind')} ${PROP('Dim.result')}")],
            w=12,
            stack=True,
        )
    )
    p.append(
        series(
            lay,
            "Rows by state",
            [search("a", ["kind", "state"], "payments_records", stat="Maximum", label="${PROP('Dim.kind')} ${PROP('Dim.state')}")],
            w=12,
            desc="Gauges read from the database at scrape time; correct after a restart.",
        )
    )
    p.append(
        series(
            lay,
            "Oldest unresolved row age",
            [search("a", ["kind", "state"], "payments_oldest_age_seconds", stat="Maximum", label="${PROP('Dim.kind')} ${PROP('Dim.state')}")],
            unit="s",
            w=12,
            lines=[(600, "orange")],
            desc="Orange line: ADR 0009 'unknown too long' (10 min for payments).",
        )
    )
    p.append(
        series(
            lay,
            "Adapter calls by op and result",
            [search("a", ["op", "result"], "payments_adapter_calls_total", label="${PROP('Dim.op')} ${PROP('Dim.result')}")],
            w=12,
            stack=True,
        )
    )
    p.append(
        series(
            lay,
            "Payouts kill switch (1 on, 0 tripped)",
            [search("a", [], "payments_payouts_enabled", stat="Minimum", label="payouts enabled")],
            unit="none",
            w=6,
        )
    )
    p.append(
        series(
            lay,
            "Anomalies",
            [search("a", ["kind", "severity"], "payments_anomalies_total", label="${PROP('Dim.kind')} ${PROP('Dim.severity')}")],
            w=6,
            lines=[(1, "red")],
            no_value="0 (none)",
        )
    )
    return dashboard(
        "tillflow-payments",
        "TillFlow Payments",
        "Payments RED, saturation and money-path signals (ADR 0009). Generated by infra/grafana/build.py.",
        p,
    )


def main() -> None:
    for board in (overview(), pos(), payments()):
        path = HERE / f"{board['uid'].removeprefix('tillflow-')}.json"
        path.write_text(json.dumps(board, indent=2) + "\n")
        print(f"wrote {path.relative_to(HERE.parent.parent)} ({len(board['panels'])} panels)")


if __name__ == "__main__":
    main()
