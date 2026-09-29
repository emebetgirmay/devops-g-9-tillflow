# G3 app-level alarms from Payments' own metrics (ADR 0009 section 4, ADR 0010 R-6), on the same
# SNS topic as the platform alarms (alerts.tf), firing AND recovered.
#
# CloudWatch alarms cannot use SEARCH(), and an exact-dimension alarm misses the OTelLib
# dimension ADOT's EMF exporter adds (it would never fire). So each alarm is one Metrics Insights
# query: `FROM TillFlow` matches every dimension set, and WHERE filters on the metric's own labels.
# Gauges come from the database and are identical across tasks, so they aggregate with MAX/MIN,
# never SUM (ADR 0009 section 1); counters arrive as per-period increases and aggregate with SUM.

locals {
  payments_app_alarms = {
    "critical-anomaly" = {
      query      = "SELECT SUM(payments_anomalies_total) FROM TillFlow WHERE severity = 'critical'"
      comparison = "GreaterThanThreshold"
      threshold  = 0
      period     = 60
      evaluate   = 1
      missing    = "notBreaching"
      severity   = "page"
      symptom    = "Critical money-path anomaly (contradiction or constraint violation)"
      slo_impact = "Hard invariant: duplicate charge or payout is a P0"
      action     = "Freeze payouts with the kill switch, inspect the anomaly row, never re-send"
      owner      = "@chesangJ"
    }
    "payouts-paused" = {
      query      = "SELECT MIN(payments_payouts_enabled) FROM TillFlow"
      comparison = "LessThanThreshold"
      threshold  = 1
      period     = 60
      evaluate   = 2
      missing    = "notBreaching"
      severity   = "page"
      symptom    = "Payouts kill switch tripped"
      slo_impact = "Commission: payouts stop until re-enabled; 06:30 EAT target at risk"
      action     = "Read the trip reason, fix the cause, re-enable by hand"
      owner      = "@chesangJ"
    }
    "needs-review" = {
      query      = "SELECT MAX(payments_records) FROM TillFlow WHERE state = 'NEEDS_REVIEW'"
      comparison = "GreaterThanThreshold"
      threshold  = 0
      period     = 300
      evaluate   = 3
      missing    = "notBreaching"
      severity   = "slack"
      symptom    = "Payments or payouts waiting in NEEDS_REVIEW for 15 minutes"
      slo_impact = "Unresolved money state; counts against the Payments and Commission SLOs"
      action     = "Resolve with provider evidence (M-PESA Organization Portal); do not auto-fail"
      owner      = "@chesangJ"
    }
    "payment-unknown-too-long" = {
      query      = "SELECT MAX(payments_oldest_age_seconds) FROM TillFlow WHERE kind = 'payment' AND state = 'UNKNOWN'"
      comparison = "GreaterThanThreshold"
      threshold  = 600
      period     = 60
      evaluate   = 2
      missing    = "notBreaching"
      severity   = "slack"
      symptom    = "A payment has been UNKNOWN for over 10 minutes"
      slo_impact = "Payments SLO: accepted-and-terminal-within-60s budget"
      action     = "Run the reconcile pass; leave it pending, never resend"
      owner      = "@chesangJ"
    }
    "payout-unknown-too-long" = {
      query      = "SELECT MAX(payments_oldest_age_seconds) FROM TillFlow WHERE kind = 'disbursement' AND state = 'UNKNOWN'"
      comparison = "GreaterThanThreshold"
      threshold  = 600
      period     = 60
      evaluate   = 2
      missing    = "notBreaching"
      severity   = "slack"
      symptom    = "A payout has been UNKNOWN for over 10 minutes"
      slo_impact = "Commission: payout must be terminal by 06:30 EAT"
      action     = "Run the reconcile pass; never resubmit a payout (ADR 0008)"
      owner      = "@chesangJ"
    }
    "reconcile-stale" = {
      query      = "SELECT SUM(payments_reconcile_runs_total) FROM TillFlow WHERE result = 'ok'"
      comparison = "LessThanThreshold"
      threshold  = 1
      period     = 300
      evaluate   = 3
      # No successful run recorded at all is exactly the failure this watches for.
      missing    = "breaching"
      severity   = "slack"
      symptom    = "No successful reconcile pass in 15 minutes"
      slo_impact = "UNKNOWN payments and payouts stop resolving; Payments and Commission SLOs"
      action     = "Run POST /_admin/sweep by hand from inside the VPC; check what schedules it"
      owner      = "@emebetgirmay"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "payments_app" {
  for_each = local.payments_app_alarms

  alarm_name          = "${var.name_prefix}-payments-${each.key}"
  comparison_operator = each.value.comparison
  threshold           = each.value.threshold
  evaluation_periods  = each.value.evaluate
  datapoints_to_alarm = each.value.evaluate
  treat_missing_data  = each.value.missing
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "q"
    expression  = each.value.query
    period      = each.value.period
    label       = each.key
    return_data = true
  }

  alarm_description = jsonencode({
    environment       = var.environment
    service           = "payments"
    symptom           = "${each.value.symptom} (${each.value.severity})"
    slo_impact        = each.value.slo_impact
    observed          = "${each.value.query} ${each.value.comparison} ${each.value.threshold}"
    grafana_panel     = var.grafana_url == "" ? "" : "${var.grafana_url}/d/tillflow-payments"
    runbook           = "${local.runbook_url}#payments-${each.key}"
    owner             = each.value.owner
    first_safe_action = each.value.action
  })

  tags = { service = "payments" }
}
