#!/usr/bin/env bash
# Cost evidence for docs/cost-model.md: what TillFlow costs, measured rather than guessed.
#
# The AWS account is shared: other teams run in other regions, and eu-north-1 also holds other
# stacks. Cost Explorer cannot split them (the `group` tag is not active for cost allocation), so:
#
#   1. Cost Explorer gives the eu-north-1 bill per usage type, and so the unit price actually paid.
#   2. TillFlow's cost is built bottom-up: its own resources (tag group=devops-g9) and its own
#      CloudWatch usage figures, times those unit prices.
#   3. Other stacks in the region are costed the same way, so the two can be reconciled with the
#      measured eu-north-1 bill.
#
#   cost-by-usage-type.json   eu-north-1 cost and quantity per usage type, and the unit price
#   cost-daily.json           eu-north-1 cost per day and AWS service
#   cost-drivers.json         what TillFlow and the other stacks run, read from AWS
#   cost-model.json           monthly cost lines, totals and the reconciliation
#
# Usage only: credits and refunds are excluded. Monthly = 730 hours.
#
# Read-only. Needs an SSO session with Cost Explorer read access: aws sso login --profile g9
#
#   ./evidence/platform-delivery/cost/collect-cost.sh                    # last 14 full days
#   COST_FROM=2026-09-16 COST_TO=2026-09-30 ./evidence/platform-delivery/cost/collect-cost.sh

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-g9}" AWS_REGION="${AWS_REGION:-eu-north-1}"
COST_TO="${COST_TO:-$(date -u +%F)}"                     # exclusive: today is not complete yet
COST_FROM="${COST_FROM:-$(date -u -d "$COST_TO 14 days ago" +%F)}"
OUT="$(cd "$(dirname "$0")" && pwd)"

echo "cost window: $COST_FROM -> $COST_TO (end exclusive), region $AWS_REGION"

python3 - "$OUT" "$COST_FROM" "$COST_TO" "$AWS_REGION" <<'PY'
import json, subprocess, sys
from collections import defaultdict
from datetime import date
from pathlib import Path

out, start, end, region = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
PREFIX = GROUP = "devops-g9"
HOURS = 730                                   # hours in an average month
days = (date.fromisoformat(end) - date.fromisoformat(start)).days
MONTH = HOURS / 24 / days                     # window -> month
REGION_CODE = {"eu-north-1": "EUN1"}[region]  # usage-type prefix in the bill

# AWS list prices for items the window's bill does not show (free tier, or too small to price).
LIST_PRICE = {
    "kms_key_month": 1.00,
    "apigw_http_request": 1.00 / 1e6,
    "lambda_request": 0.20 / 1e6,
    "lambda_gb_second": 0.0000166667,
    "logs_storage_gb_month": 0.03,
}


def aws(*args, region_override=None):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region_override] if region_override else [])
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout or "null")


def ce(granularity, group_by, flt):
    """get-cost-and-usage, all pages (the CLI does not page this call)."""
    results, token = [], None
    while True:
        args = ["ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
                "--granularity", granularity, "--metrics", "UnblendedCost", "UsageQuantity",
                "--group-by", *[json.dumps(g) for g in group_by], "--filter", json.dumps(flt)]
        if token:
            args += ["--next-page-token", token]
        page = aws(*args, region_override="us-east-1")
        results += page["ResultsByTime"]
        token = page.get("NextPageToken")
        if not token:
            return results


def metric_sum(namespace, name, dims, stat="Sum"):
    """Total of a CloudWatch metric over the window (daily datapoints, summed)."""
    points = aws("cloudwatch", "get-metric-statistics", "--namespace", namespace, "--metric-name", name,
                 "--dimensions", *[f"Name={k},Value={v}" for k, v in dims.items()],
                 "--start-time", f"{start}T00:00:00Z", "--end-time", f"{end}T00:00:00Z",
                 "--period", "86400", "--statistics", stat)["Datapoints"]
    return sum(p[stat] for p in points)


def tag(resource, key):
    return next((t["Value"] for t in resource.get("Tags", []) if t["Key"] == key), None)


# --- 1. The eu-north-1 bill and the unit prices in it --------------------------------------
try:
    tags = aws("ce", "list-cost-allocation-tags", "--tag-keys", "group", region_override="us-east-1")["CostAllocationTags"]
    tag_active = any(t["TagKey"] == "group" and t["Status"] == "Active" for t in tags)
except subprocess.CalledProcessError:
    tag_active = False

flt = {"And": [{"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
               {"Dimensions": {"Key": "REGION", "Values": [region]}}]}
print(f"Cost Explorer: {region}, usage only; group tag active for cost allocation: {tag_active}")

by_type = defaultdict(lambda: {"cost": 0.0, "quantity": 0.0, "unit": ""})
for period in ce("MONTHLY", [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}], flt):
    for g in period["Groups"]:
        row = by_type[tuple(g["Keys"])]
        row["cost"] += float(g["Metrics"]["UnblendedCost"]["Amount"])
        row["quantity"] += float(g["Metrics"]["UsageQuantity"]["Amount"])
        row["unit"] = g["Metrics"]["UsageQuantity"].get("Unit", "")
usage_types = sorted(
    ({"service": s, "usage_type": u, "cost_usd": round(r["cost"], 4), "quantity": round(r["quantity"], 4),
      "unit": r["unit"], "unit_price_usd": round(r["cost"] / r["quantity"], 6) if r["quantity"] else None}
     for (s, u), r in by_type.items()),
    key=lambda r: -r["cost_usd"])
region_cost = sum(r["cost_usd"] for r in usage_types)
(out / "cost-by-usage-type.json").write_text(json.dumps(
    {"window": {"from": start, "to_exclusive": end, "days": days}, "region": region,
     "group_tag_active": tag_active, "total_usd": round(region_cost, 2), "usage_types": usage_types}, indent=2) + "\n")

daily = [{"date": p["TimePeriod"]["Start"],
          "services": {g["Keys"][0]: round(float(g["Metrics"]["UnblendedCost"]["Amount"]), 4) for g in p["Groups"]}}
         for p in ce("DAILY", [{"Type": "DIMENSION", "Key": "SERVICE"}], flt)]
(out / "cost-daily.json").write_text(json.dumps({"region": region, "days": daily}, indent=2) + "\n")


def price(usage_type, service=""):
    """Unit price actually paid in the window for one usage type (e.g. 'Fargate-GB-Hours')."""
    row = next((r for r in usage_types
                if r["usage_type"] == f"{REGION_CODE}-{usage_type}" and r["service"].startswith(service)), None)
    return row["unit_price_usd"] if row and row["cost_usd"] > 0 else None


# --- 2. What runs in the region, TillFlow's and not --------------------------------------
def ecs_tasks():
    rows = []
    for cluster in aws("ecs", "list-clusters")["clusterArns"]:
        arns = aws("ecs", "list-services", "--cluster", cluster)["serviceArns"]
        for i in range(0, len(arns), 10):
            for s in aws("ecs", "describe-services", "--cluster", cluster, "--services", *arns[i:i + 10])["services"]:
                td = aws("ecs", "describe-task-definition", "--task-definition", s["taskDefinition"])["taskDefinition"]
                rows.append({"cluster": cluster.rsplit("/", 1)[1], "service": s["serviceName"], "desired": s["desiredCount"],
                             "vcpu": int(td["cpu"]) / 1024, "memory_gb": int(td["memory"]) / 1024,
                             "arm": td.get("runtimePlatform", {}).get("cpuArchitecture") == "ARM64"})
    return rows


tasks = ecs_tasks()
nats = aws("ec2", "describe-nat-gateways", "--filter", "Name=state,Values=available")["NatGateways"]
lbs = aws("elbv2", "describe-load-balancers")["LoadBalancers"]
lb_tags = {}
for i in range(0, len(lbs), 20):
    for d in aws("elbv2", "describe-tags", "--resource-arns", *[lb["LoadBalancerArn"] for lb in lbs[i:i + 20]])["TagDescriptions"]:
        lb_tags[d["ResourceArn"]] = {t["Key"]: t["Value"] for t in d["Tags"]}
endpoints = aws("ec2", "describe-vpc-endpoints")["VpcEndpoints"]
enis = aws("ec2", "describe-network-interfaces", "--filters", "Name=association.public-ip,Values=*")["NetworkInterfaces"]

ours_nat = {n["NatGatewayId"] for n in nats if tag(n, "group") == GROUP}
ours_lb = {lb["LoadBalancerArn"] for lb in lbs if lb_tags.get(lb["LoadBalancerArn"], {}).get("group") == GROUP}
lb_names = {lb["LoadBalancerArn"]: lb["LoadBalancerName"] for lb in lbs}


def ip_is_ours(eni):
    desc = eni.get("Description", "")
    if any(n in desc for n in ours_nat):
        return True
    return any(f"app/{lb_names[a]}/" in desc for a in ours_lb)


def side(ours):
    return "tillflow" if ours else "other"


inventory = {"tillflow": defaultdict(float), "other": defaultdict(float)}
for t in tasks:
    inv = inventory[side(t["cluster"] == PREFIX)]
    key = "arm_" if t["arm"] else ""
    inv[f"{key}vcpu"] += t["vcpu"] * t["desired"]
    inv[f"{key}memory_gb"] += t["memory_gb"] * t["desired"]
for n in nats:
    inventory[side(n["NatGatewayId"] in ours_nat)]["nat_gateways"] += 1
for lb in lbs:
    inventory[side(lb["LoadBalancerArn"] in ours_lb)]["load_balancers"] += 1
for e in endpoints:
    if e["VpcEndpointType"] == "Interface":   # gateway endpoints (S3, DynamoDB) are free
        inventory[side(tag(e, "group") == GROUP)]["vpc_endpoint_enis"] += len(e.get("SubnetIds", []))
for eni in enis:
    inventory[side(ip_is_ours(eni))]["public_ipv4"] += 1

# TillFlow's own usage in the window.
alarms = aws("cloudwatch", "describe-alarms", "--alarm-name-prefix", PREFIX)["MetricAlarms"]
insights = [a for a in alarms if any("SELECT" in (m.get("Expression") or "").upper() for m in a.get("Metrics", []))]
metrics = {ns: len(aws("cloudwatch", "list-metrics", "--namespace", ns, "--recently-active", "PT3H")["Metrics"])
           for ns in ("TillFlow", "TillFlow/Probe")}
metrics["ECS/ContainerInsights"] = len(aws("cloudwatch", "list-metrics", "--namespace", "ECS/ContainerInsights",
                                           "--dimensions", f"Name=ClusterName,Value={PREFIX}",
                                           "--recently-active", "PT3H")["Metrics"])
log_groups = [g for p in (f"/{PREFIX}", f"/aws/lambda/{PREFIX}", f"/aws/apigateway/{PREFIX}")
              for g in aws("logs", "describe-log-groups", "--log-group-name-prefix", p)["logGroups"]]
log_in = {g["logGroupName"]: metric_sum("AWS/Logs", "IncomingBytes", {"LogGroupName": g["logGroupName"]}) for g in log_groups}
secrets = [s for s in aws("secretsmanager", "list-secrets")["SecretList"] if s["Name"].startswith(PREFIX)]
kms = [a for a in aws("kms", "list-aliases")["Aliases"] if a["AliasName"].startswith(f"alias/{PREFIX}")]
ecr = {}
for repo in (f"{PREFIX}/pos", f"{PREFIX}/payments"):
    images = aws("ecr", "describe-images", "--repository-name", repo)["imageDetails"]
    ecr[repo] = {"images": len(images), "gb": sum(i.get("imageSizeInBytes", 0) for i in images) / 1e9}
api_id = next(a["ApiId"] for a in aws("apigatewayv2", "get-apis")["Items"] if a["Name"] == f"{PREFIX}-api")
api_requests = metric_sum("AWS/ApiGateway", "Count", {"ApiId": api_id})
lambdas = [f for f in aws("lambda", "list-functions")["Functions"] if f["FunctionName"].startswith(PREFIX)]
lambda_calls = {f["FunctionName"]: metric_sum("AWS/Lambda", "Invocations", {"FunctionName": f["FunctionName"]}) for f in lambdas}
lambda_gb_s = sum(metric_sum("AWS/Lambda", "Duration", {"FunctionName": f["FunctionName"]}) / 1000 * f["MemorySize"] / 1024
                  for f in lambdas)


def nat_gb(nat_ids):
    return sum(metric_sum("AWS/NATGateway", m, {"NatGatewayId": n}) for n in nat_ids
               for m in ("BytesInFromSource", "BytesInFromDestination")) / 1e9


def lcus(arns):
    return sum(metric_sum("AWS/ApplicationELB", "ConsumedLCUs", {"LoadBalancer": a.split(":loadbalancer/")[1]})
               for a in arns if lb_names[a] and "/app/" in a)


other_nat = {n["NatGatewayId"] for n in nats} - ours_nat
other_lb = set(lb_names) - ours_lb
usage = {
    "tillflow": {"nat_gb": nat_gb(ours_nat), "lcu_hours": lcus(ours_lb)},
    "other": {"nat_gb": nat_gb(other_nat), "lcu_hours": lcus(other_lb)},
}

drivers = {
    "window": {"from": start, "to_exclusive": end, "days": days},
    "inventory_now": {k: dict(v) for k, v in inventory.items()},
    "ecs_tasks": tasks,
    "tillflow": {
        "alarms": {"total": len(alarms), "metrics_insights": len(insights)},
        "custom_metrics_recently_active": metrics,
        "log_groups": [{"name": g["logGroupName"], "retention_days": g.get("retentionInDays"),
                        "stored_mb": round(g.get("storedBytes", 0) / 1e6, 2),
                        "ingested_mb_in_window": round(log_in[g["logGroupName"]] / 1e6, 2)} for g in log_groups],
        "secrets": len(secrets), "kms_keys": len(kms),
        "ecr": {k: {"images": v["images"], "gb": round(v["gb"], 3)} for k, v in ecr.items()},
        "api_requests_in_window": api_requests,
        "lambda_invocations_in_window": lambda_calls,
        "lambda_gb_seconds_in_window": round(lambda_gb_s, 1),
    },
    "usage_in_window": usage,
    "other_stacks": {
        "load_balancers": [lb_names[a] for a in sorted(other_lb)],
        "nat_gateways": [tag(n, "Name") or n["NatGatewayId"] for n in nats if n["NatGatewayId"] in other_nat],
        "interface_vpc_endpoints": [tag(e, "Name") or e["ServiceName"] for e in endpoints
                                    if e["VpcEndpointType"] == "Interface" and tag(e, "group") != GROUP],
    },
}
(out / "cost-drivers.json").write_text(json.dumps(drivers, indent=2) + "\n")


# --- 3. Monthly cost lines -----------------------------------------------------------------
def line(stack, component, basis, quantity, unit_price, source):
    return {"stack": stack, "component": component, "basis": basis, "monthly_quantity": round(quantity, 3),
            "unit_price_usd": unit_price, "price_source": source,
            "monthly_usd": None if unit_price is None else round(quantity * unit_price, 2)}


def always_on(stack, inv):
    rows = []
    for key, component, usage_type, unit in (
        ("vcpu", "Fargate vCPU (x86)", "Fargate-vCPU-Hours:perCPU", "vCPU-h"),
        ("memory_gb", "Fargate memory (x86)", "Fargate-GB-Hours", "GB-h"),
        ("arm_vcpu", "Fargate vCPU (ARM)", "Fargate-ARM-vCPU-Hours:perCPU", "vCPU-h"),
        ("arm_memory_gb", "Fargate memory (ARM)", "Fargate-ARM-GB-Hours", "GB-h"),
        ("nat_gateways", "NAT gateway hours", "NatGateway-Hours", "h"),
        ("load_balancers", "Load balancer hours", "LoadBalancerUsage", "h"),
        ("vpc_endpoint_enis", "Interface VPC endpoints (per AZ)", "VpcEndpoint-Hours", "h"),
        ("public_ipv4", "Public IPv4 addresses", "PublicIPv4:InUseAddress", "h"),
    ):
        if inv.get(key):
            rows.append(line(stack, component, f"{inv[key]:g} x {HOURS} {unit}", inv[key] * HOURS, price(usage_type), "bill"))
    return rows


lines = always_on("tillflow", inventory["tillflow"])
lines += [
    line("tillflow", "NAT data processed", f"{usage['tillflow']['nat_gb']:.2f} GB in window",
         usage["tillflow"]["nat_gb"] * MONTH, price("NatGateway-Bytes"), "bill"),
    line("tillflow", "Load balancer LCUs", f"{usage['tillflow']['lcu_hours']:.2f} LCU-h in window",
         usage["tillflow"]["lcu_hours"] * MONTH, price("LCUUsage"), "bill"),
    line("tillflow", "CloudWatch custom metrics (ADOT, probe, Container Insights)", f"{sum(metrics.values())} metrics",
         sum(metrics.values()), price("CW:MetricMonitorUsage"), "bill"),
    line("tillflow", "CloudWatch alarms (standard)", f"{len(alarms) - len(insights)} alarms",
         len(alarms) - len(insights), price("CW:AlarmMonitorUsage"), "bill"),
    line("tillflow", "CloudWatch Metrics Insights alarms", f"{len(insights)} alarms",
         len(insights), price("CW:MetricInsightAlarmUsage"), "bill"),
    line("tillflow", "CloudWatch Logs ingestion", f"{sum(log_in.values()) / 1e9:.3f} GB in window",
         sum(log_in.values()) / 1e9 * MONTH, price("DataProcessing-Bytes"), "bill"),
    line("tillflow", "CloudWatch Logs storage", f"{sum(g.get('storedBytes', 0) for g in log_groups) / 1e9:.3f} GB stored",
         sum(g.get("storedBytes", 0) for g in log_groups) / 1e9, LIST_PRICE["logs_storage_gb_month"], "list"),
    line("tillflow", "Secrets Manager", f"{len(secrets)} secrets", len(secrets), price("AWSSecretsManager-Secrets"), "bill"),
    line("tillflow", "KMS customer-managed keys", f"{len(kms)} keys", len(kms), LIST_PRICE["kms_key_month"], "list"),
    line("tillflow", "ECR image storage", f"{sum(v['gb'] for v in ecr.values()):.2f} GB, {sum(v['images'] for v in ecr.values())} images",
         sum(v["gb"] for v in ecr.values()), price("TimedStorage-ByteHrs", "Amazon EC2 Container Registry"), "bill"),
    line("tillflow", "API Gateway HTTP requests", f"{api_requests:.0f} requests in window",
         api_requests * MONTH, LIST_PRICE["apigw_http_request"], "list"),
    line("tillflow", "Lambda (probe, sweep, notifier)", f"{sum(lambda_calls.values()):.0f} calls, {lambda_gb_s:.0f} GB-s in window",
         1, (sum(lambda_calls.values()) * LIST_PRICE["lambda_request"] + lambda_gb_s * LIST_PRICE["lambda_gb_second"]) * MONTH,
         "list (free tier covers it today)"),
]
other = always_on("other", inventory["other"])
other += [
    line("other", "NAT data processed", f"{usage['other']['nat_gb']:.2f} GB in window",
         usage["other"]["nat_gb"] * MONTH, price("NatGateway-Bytes"), "bill"),
    line("other", "Load balancer LCUs", f"{usage['other']['lcu_hours']:.2f} LCU-h in window",
         usage["other"]["lcu_hours"] * MONTH, price("LCUUsage"), "bill"),
]

tillflow_total = round(sum(l["monthly_usd"] or 0 for l in lines), 2)
other_total = round(sum(l["monthly_usd"] or 0 for l in other), 2)
measured = round(region_cost * MONTH, 2)
model = {
    "window": {"from": start, "to_exclusive": end, "days": days}, "region": region,
    "tillflow": lines, "other_stacks": other,
    "tillflow_monthly_usd": tillflow_total,
    "other_stacks_monthly_usd": other_total,
    "region_measured_monthly_usd": measured,
    # What neither side explains: other stacks' logs and metrics, resources that ran for part of
    # the window only (k6 tasks, deleted stacks), and data transfer.
    "unexplained_monthly_usd": round(measured - tillflow_total - other_total, 2),
}
(out / "cost-model.json").write_text(json.dumps(model, indent=2) + "\n")


def table(rows):
    print("| Component | Basis | Unit price (USD) | Price from | Monthly (USD) |\n|---|---|---|---|---|")
    for r in rows:
        p = "" if r["unit_price_usd"] is None else f"{r['unit_price_usd']:.6g}"
        m = "n/a" if r["monthly_usd"] is None else f"{r['monthly_usd']:.2f}"
        print(f"| {r['component']} | {r['basis']} | {p} | {r['price_source']} | {m} |")


print("\nTillFlow")
table(lines)
print(f"\nOther stacks in {region}: {', '.join(drivers['other_stacks']['load_balancers'] + drivers['other_stacks']['nat_gateways'])}")
table(other)
print(f"\nTillFlow {tillflow_total} + other stacks {other_total} = {round(tillflow_total + other_total, 2)} USD/month;"
      f" measured {region} bill {measured} USD/month; unexplained {model['unexplained_monthly_usd']}")
PY
