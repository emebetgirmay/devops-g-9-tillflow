# Shared libraries

M-Pesa port + fake adapter, OTel helpers, Docker base images. Owned primarily by Payments; Product co-owns shared contracts.

Currently implemented: the M-Pesa port skeleton and the FakeAdapter (`mpesa/`). OTel helpers and Docker base images are not here yet.

## No real Daraja calls

**Nothing in this folder calls Daraja or Safaricom, and nothing here may.** There is no HTTP client, no credential and no Safaricom URL. `tests/test_no_daraja_guard.py` fails the build if any of these appear. Commission never calls Daraja at all; only the deployed Payments sandbox adapter does ([ADR 0004](../../docs/adrs/0004-mpesa-adapter.md)). No real adapter is implemented here.

## The port

`mpesa/port.py` defines `MpesaPort`, typed by `mpesa/models.py`:

| Method | Purpose |
|---|---|
| `initiate_charge(ChargeRequest) -> ChargeAccepted` | Start a charge. Every request carries an `idempotency_key`. Call at most once per key. |
| `query_status(provider_ref) -> PaymentStatus` | Ask for the state of a charge (reconciliation). |
| `parse_callback(headers, body) -> CallbackEvent` | Verify authenticity, then parse a callback into a normalised event. |

Errors follow [ADR 0006](../../docs/adrs/0006-idempotency-replay.md): `ChargeDeclinedError` means definitively declined (no charge exists); `OutcomeUnknownError` means timeout or transport failure (a charge may exist, so never treat it as a decline and never retry initiate). Provider result codes are mapped once in `mpesa/result_codes.py`, and unrecognised codes are `UNKNOWN`. Only codes 0 and 1032 are verified (Daraja STK Query page); the rest are marked unverified in the ADR 0006 mapping table.

Not in the skeleton yet: auth and B2C (listed in ADR 0004), to be added with G2.

## FakeAdapter

`mpesa/fake_adapter.py`. Deterministic: no network, no credentials, no randomness. Provider references and receipts are derived from the idempotency key. Time comes from an injected clock, so tests use `ManualClock` and advance it explicitly.

### Magic MSISDNs

The scenario is chosen by the customer phone number. These numbers (`254` plus nine digits starting with `0`) are not valid Kenyan mobiles, so they cannot belong to a real person. Any other well-formed number behaves as `SUCCESS`.

| MSISDN | Scenario | Behaviour |
|---|---|---|
| `254000000001` | `SUCCESS` | Success callback after `callback_delay_s` (default 1 s) |
| `254000000002` | `INSUFFICIENT_FUNDS` | Decline callback |
| `254000000003` | `USER_CANCELLED` | Decline callback |
| `254000000004` | `WRONG_PIN` | Decline callback |
| `254000000005` | `PROMPT_EXPIRED` | Callback mapping to `EXPIRED` (not a decline) |
| `254000000006` | `TIMEOUT_NO_CALLBACK` | Accepted, no callback ever, query stays `UNKNOWN` |
| `254000000007` | `TIMEOUT_QUERY_RESOLVES` | Accepted, no callback, query returns success after `late_after_s` (default 120 s) |
| `254000000008` | `DELAYED_CALLBACK` | Success callback only after `late_after_s` |
| `254000000009` | `DUPLICATE_CALLBACK` | Same success callback delivered `duplicate_count` times (default 3), byte-identical |
| `254000000010` | `OUT_OF_ORDER_SUCCESS_THEN_FAILURE` | Success, then a contradicting failure 1 s later |
| `254000000011` | `OUT_OF_ORDER_FAILURE_THEN_SUCCESS` | Failure, then a contradicting success 1 s later |
| `254000000012` | `INITIATE_TIMEOUT` | `initiate_charge` raises `OutcomeUnknownError`; a success callback for an unknown reference arrives after `late_after_s` |
| `254000000013` | `INITIATE_REJECTED` | `initiate_charge` raises `ChargeDeclinedError` |
| `254000000014` | `UNRECOGNISED_CODE` | Callback with a code outside the mapping, must resolve to `UNKNOWN` |

`FakeAdapter` refuses a second `initiate_charge` for the same idempotency key (`DuplicateInitiateError`), because a real provider would charge twice. `initiate_call_count` lets tests assert exactly one provider call.

### Driving callbacks

```python
from mpesa import ChargeRequest, FakeAdapter, ManualClock

clock = ManualClock()
adapter = FakeAdapter(clock=clock)
adapter.initiate_charge(ChargeRequest("idem-key-0000000001", "tenant-a", "254000000009", 150000))
clock.advance(1)
for delivery in adapter.due_callbacks():  # handed out once, in delivery order
    # POST delivery.body with delivery.headers to the callback handler under test,
    # or call adapter.parse_callback(delivery.headers, delivery.body) directly.
    ...
```

`scheduled_callbacks(provider_ref)` returns every callback for a reference regardless of the clock. Callback bodies follow the documented Daraja STK callback (`Body.stkCallback`, verified against the portal's M-Pesa Express page); `CallbackMetadata` appears only on success. Authenticity is simulated with an HMAC over the body using a fixed public test key in header `X-Fake-Signature`. This is a stand-in that exercises the verify-then-parse path, not a model of how Daraja authenticates callbacks.

### Using it in CI and k6

- CI and k6 configure Payments with the FakeAdapter, never the sandbox adapter, and need no Safaricom credentials.
- Unit and contract tests import `mpesa` directly (Python).
- k6 cannot import Python. It drives the Payments HTTP API, which runs against the FakeAdapter, using the magic MSISDNs above. Exposing `due_callbacks()` over HTTP for k6 is part of the G2 Payments test harness and is not built here.

## Tests

From this folder, with stdlib only:

```bash
python3 -m unittest discover -s tests -t .
```
