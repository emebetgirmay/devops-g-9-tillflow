# Cost evidence

Written up in [`docs/cost-model.md`](../../../docs/cost-model.md). Collected 2026-09-30 for the bill
window 2026-09-16 → 2026-09-29, read-only, with [`collect-cost.sh`](collect-cost.sh).

| File | What it holds |
|---|---|
| `cost-by-usage-type.json` | `eu-north-1` cost and quantity per usage type, and the unit price each implies |
| `cost-daily.json` | `eu-north-1` cost per day and AWS service |
| `cost-drivers.json` | What TillFlow and the other stack in the region run, and TillFlow's usage in the window |
| `cost-model.json` | Monthly cost lines for both, totals and the reconciliation with the bill |

The account is shared, so these files contain only `eu-north-1` figures and TillFlow's own
resources. The other stack's resources are listed by name only.
