# Evidence: Daraja sandbox B2C contract test (ADR 0008 open question 1)

DRI: Mitingi Joy Chesang (`@chesangJ`). Run 2026-09-29 against the deployed Payments service
(`MPESA_ADAPTER=daraja_sandbox`), KSh 10 per payout through `POST /payouts`.

| Run | Setup | Result |
|---|---|---|
| 1 | Allowlist empty | Daraja accepted (`ConversationID` issued); result callback refused and logged from `196.201.212.69` |
| 2 | `.69` allowlisted | Accepted; Daraja never sent a result. Same-key replays `200 Idempotent-Replayed`, a second key `409 payout_already_requested`: no double pay |
| 3 | Party A `600992` (docs sample) | Result accepted: `CONFIGURATION`, payout `FAILED`, kill switch tripped, next send `503 payouts_disabled` |
| 4 | Party A `600977` (portal test credentials) | Accepted; result from a second address `196.201.212.138`, refused (retried once) |
| 5 | `.138` allowlisted | Result accepted: `CONFIGURATION`, `FAILED`, kill switch tripped |

Proven live: OAuth and the B2C request against Daraja, the caller-address header at the edge
(`x-tillflow-source-ip`), the observed-IP allowlist, no double pay on replay or a second key, and
the `CONFIGURATION` fail-safe with the kill switch.

Not yet proven: a `SUCCEEDED` payout. Run 5 still failed `CONFIGURATION` with the correct Party A,
which points at the stored security credential not matching the `testapi` initiator password.
Next: regenerate it on the portal's Test Credentials page from that password, update
`devops-g9/daraja`, redeploy Payments on `daraja_sandbox`, and send one more payout.

Gaps found: the exact Daraja result code is stored but neither logged nor returned by
`GET /payouts`, and the kill switch lives in the container's SQLite, so any redeploy resets it
(persistent state lands with RDS, ADR 0002).
