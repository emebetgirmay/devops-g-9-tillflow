"""CloudWatch alarm (via SNS) -> Slack. ADR 0010 section 4.

The webhook is read from Secrets Manager on every invoke, so rotating it needs no deploy, and it
is never in Terraform, Git or this function's environment. Until someone sets the secret's value
the function logs and drops the message rather than failing (the alarm itself is unaffected).

Each alarm's `alarm_description` is JSON carrying the runbook's alert contract
(docs/runbook.md, "Alert contract"); missing fields fall back to the alarm's own data.
"""

from __future__ import annotations

import json
import os
import urllib.request

CONTRACT_FIELDS = (
    "environment",
    "service",
    "symptom",
    "slo_impact",
    "observed",
    "grafana_panel",
    "runbook",
    "owner",
    "first_safe_action",
)

_secrets = None


def _secrets_client():
    global _secrets
    if _secrets is None:
        import boto3  # in the Lambda runtime; imported lazily so tests need no AWS SDK

        _secrets = boto3.client("secretsmanager")
    return _secrets


def webhook_url(client=None) -> str:
    """The webhook, or "" when the secret has no value yet or holds a placeholder."""
    client = client or _secrets_client()
    try:
        raw = client.get_secret_value(SecretId=os.environ["SLACK_SECRET_ID"])["SecretString"]
    except client.exceptions.ResourceNotFoundException:
        return ""
    try:
        url = json.loads(raw).get("url", "")
    except (json.JSONDecodeError, AttributeError):
        return ""
    return url if url.startswith("https://hooks.slack.com/") else ""


def contract(alarm: dict) -> dict:
    """The alert contract for one alarm, from its description with fallbacks."""
    try:
        desc = json.loads(alarm.get("AlarmDescription") or "{}")
    except json.JSONDecodeError:
        desc = {"symptom": alarm.get("AlarmDescription")}
    if not isinstance(desc, dict):
        desc = {}
    fields = {k: str(desc.get(k) or "").strip() for k in CONTRACT_FIELDS}
    fields["environment"] = fields["environment"] or os.environ.get("ENVIRONMENT", "sandbox")
    fields["service"] = fields["service"] or "unknown"
    fields["symptom"] = fields["symptom"] or alarm.get("AlarmName", "alarm")
    fields["observed"] = fields["observed"] or alarm.get("NewStateReason", "")
    fields["grafana_panel"] = fields["grafana_panel"] or os.environ.get("GRAFANA_URL", "")
    fields["runbook"] = fields["runbook"] or os.environ.get("RUNBOOK_URL", "")
    fields["owner"] = fields["owner"] or "see CODEOWNERS"
    fields["first_safe_action"] = fields["first_safe_action"] or "see runbook"
    fields["slo_impact"] = fields["slo_impact"] or "not stated"
    return fields


def slack_message(alarm: dict) -> dict:
    state = alarm.get("NewStateValue", "")
    firing = state == "ALARM"
    c = contract(alarm)
    status = "FIRING" if firing else "RECOVERED" if state == "OK" else state or "UNKNOWN"
    title = f"[{c['environment']}] {c['service']} {status}: {c['symptom']}"
    rows = [
        ("SLO impact", c["slo_impact"]),
        ("Observed", c["observed"]),
        ("Grafana panel", c["grafana_panel"]),
        ("Runbook", c["runbook"]),
        ("Owner", c["owner"]),
        ("First safe action", c["first_safe_action"]),
        ("Alarm", alarm.get("AlarmName", "")),
    ]
    return {
        "text": title,
        "attachments": [
            {
                "color": "#d93025" if firing else "#2eb886",
                "fields": [{"title": k, "value": v, "short": False} for k, v in rows if v],
            }
        ],
    }


def _post(url: str, payload: dict) -> int:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed Slack host)
        return resp.status


def handler(event, _context, client=None, post=_post):
    url = webhook_url(client)
    if not url:
        print(json.dumps({"event": "slack_webhook_not_set", "dropped": len(event.get("Records", []))}))
        return {"posted": 0, "reason": "webhook not set"}

    posted = 0
    for record in event.get("Records", []):
        raw = (record.get("Sns") or {}).get("Message", "")
        try:
            alarm = json.loads(raw)
        except json.JSONDecodeError:
            alarm = {"AlarmName": "non-alarm message", "AlarmDescription": raw}
        if not isinstance(alarm, dict):
            continue
        status = post(url, slack_message(alarm))
        print(
            json.dumps(
                {
                    "event": "slack_posted",
                    "alarm": alarm.get("AlarmName"),
                    "state": alarm.get("NewStateValue"),
                    "status": status,
                }
            )
        )
        posted += 1
    return {"posted": posted}
