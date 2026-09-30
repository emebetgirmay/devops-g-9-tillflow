# Evidence — Reliability + operations

DRI: Emebet Girmay (`@emebetgirmay`)

## G0
- [x] Draft SLOs published (`docs/slo-error-budgets.md`)

## G3
- **Start here:** [`g3-evidence.md`](g3-evidence.md): Grafana, alarms and the Slack path, real
  incidents and the drill, k6, what is not claimed, sign-off.
- [`k6-analysis.md`](k6-analysis.md): envelope, three runs, findings, capacity statement.
- Scripts: [`run-k6.sh`](run-k6.sh) (k6 inside the VPC), [`collect-drill.sh`](collect-drill.sh)
  (drill evidence as JSON with pass/fail checks), [`collect-probe.sh`](collect-probe.sh) (edge
  probe history, every minute, before CloudWatch rolls it up).

## Later
- G4: [`g4-game-day.md`](g4-game-day.md): the game-day plan (restore with RTO/RPO, Payments task
  killed, database reboot, poisoned callback), results filled on the day.
