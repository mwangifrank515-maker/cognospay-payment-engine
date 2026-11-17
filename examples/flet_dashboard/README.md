# Engine Lab: a Flet dashboard that shows how the engine works

The **real engine**, a **simulated Safaricom**, an **in-memory database**. No credentials, no network.
Pick a scenario, watch the payment travel through the pipeline, and watch the tables change one step at a time.

```bash
pip install "cognospay-engine[demo]"            # installs flet 0.86.x
python examples/flet_dashboard/app.py           # desktop window
flet run --web examples/flet_dashboard/app.py   # or in a browser (see the note on FastAPI below)
```

## What you see

| Area | Shows |
|---|---|
| **Scenarios** (left) | 12 situations, tagged *Happy path / Attack / Failure / Recovery* |
| **How a payment moves** | Link → STK push → Safaricom → callback → verify → SUCCESS → your hook → B2B payout → result, plus the reconciler as a safety net. Stages the scenario touched light up; the brightest is where it is now. |
| **Metrics + status chips** | STK pushes, payments SUCCESS, **B2B payouts sent** (the number that must never exceed the successes), alerts, items needing a human |
| **What is happening** | A narrated story. Every line is read back from engine state after each call, so if the engine behaved differently the story would say so. |
| **Safaricom simulator** | Flip how the fake Safaricom answers (STK push / verification query / B2B payout) and drive things by hand: send a push, deliver a success / cancelled / wrong-amount / no-amount callback, report a payout result, run the reconciler, move time forward |
| **Tables** | Payments, payouts, the raw callback inbox (with outcomes), the Safaricom wire log (→ out, ← in), hook and alert events, and the needs-attention report |

Playback speed (Instant / Fast / Normal / Slow) is in the header. **Reset lab** starts over.

## Scenarios

| | Proves |
|---|---|
| Happy path | The full flow, with the fee split (1,000 = 20 fee + 980 net) |
| Safaricom repeats a callback | Three identical callbacks → one credit, one hook, one payout |
| Wrong amount | KES 1 reported for a KES 100 payment → `FAILED amount_mismatch`, alert, no payout |
| Forged SUCCESS | Fake success, Safaricom says unpaid → `verification_failed`, no payout |
| Forged FAILURE | Fake "cancelled" for a payment that really succeeded → overruled, settled, alerted |
| Single-use link | Second payer refused; a rejected push gives the use back |
| STK spam to one phone | Five spellings of one number are one phone to the throttle |
| STK request times out | `unconfirmed`, not failed; the late callback is adopted by phone + amount |
| Callback never arrives | Reconciler asks Safaricom and settles; the real callback later backfills the receipt |
| Crash before payout | Paid customer, no payout row → reconciler creates exactly one |
| Payout times out | Ambiguous (money may have moved) → never retried, flagged for a human |
| Payout rejected | Bounded automatic retries, then a human fixes the credential and retries by hand |

## How it is built

- `lab.py`: **no Flet import.** `Lab` owns an in-memory SQLite database, a `PaymentEngine`, and `SimGateway` (a scriptable Safaricom built on `cognospay_engine.testing.FakeGateway`). Each scenario is a method that calls the public engine API and records `Step`s, each carrying a snapshot of the database. Fully unit-tested (`tests/test_flet_dashboard.py`).
- `app.py`: the Flet UI. It replays a scenario's steps with a delay and renders each step's snapshot. Handlers are `async` so playback never blocks the UI.

Add a scenario: write `sc_<key>(self)` on `Lab`, using `self._start`, `self._callback`, `self._reconcile`, `self.fast_forward`, `self.say(...)`; append a `Scenario(...)` to `SCENARIOS`; add a test asserting what it must prove.

## Notes

- Written for **Flet 0.86**, matching CognosPay's frontend. The palette is CognosPay's navy and gold.
- **Web mode and FastAPI:** `flet-web` 0.86.5 breaks on FastAPI 0.15x+ (`openapi_prefix`). If `flet run --web` fails with that error, `pip install "fastapi==0.143.0"`. Desktop mode is unaffected.
- This is a teaching tool, not an admin console. The database lives in memory and is gone when you close it.
