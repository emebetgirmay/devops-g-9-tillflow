# Out of scope for this capstone

Group: devops-g9 · TillFlow

No frontend ships in this capstone. Product decision by Alice Moraa (`@Moraaalice`), Product +
POS DRI — see [ADR 0010's Product decision on open question 1](../../docs/adrs/0010-reliability-observability.md#product-decision--question-1-2026-09-30)
for the full reasoning. In short: the flow the capstone actually grades (sale → STK → paid,
commission → B2C, replay/idempotency safety) is fully proven end to end through the API directly
— see `evidence/product-pos/` — and a rushed frontend now would be worse evidence, not better,
for the time it would cost. Web has no SLO, no Grafana row, and no alarm as a result; that's a
decision, not a gap.
