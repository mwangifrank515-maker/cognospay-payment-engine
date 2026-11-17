# Architecture

## Modules

| Module | Responsibility |
|---|---|
| `daraja.py` | HTTP client: OAuth cache, STK push, STK query, B2B. Classifies *not sent* / *unknown* / *rejected*. |
| `payments.py` | Links, throttle, STK initiation. |
| `callbacks.py` | STK + B2B callback processing, verification, adoption, `finalize_successful_payment`. |
| `payouts.py` | Claim-first disbursement and retry. |
| `inbox.py` | Raw-callback inbox, B2B result parsing/replay. |
| `reconciliation.py` | Settle stale pending, recheck failed, create missing payouts, bounded retry, needs-attention report. |
| `engine.py` | `PaymentEngine`, `Hooks`, `PayoutRoute`. |
| `models.py`, `config.py`, `fees.py`, `phone.py`, `redaction.py` | Storage, tunables, money rules, MSISDN, log hygiene. |
| `contrib/fastapi.py`, `cli.py`, `testing.py` | Adapter, CLI, test helpers. |

## Payment status

```
              STK sent, answer lost                   late SUCCESS callback (adopted by phone+amount)
   ┌──────────────▶ unconfirmed ───────────────────────────────┐
   │                                                            ▼
 create ──▶ pending ──verified SUCCESS──▶ SUCCESS          (back to pending, then verified)
              │  ▲
   verified   │  │ late SUCCESS / reconciler recovery
   failure or │  │ (never for amount_mismatch, amount_missing, verification_failed)
   rejection  ▼  │
              FAILED
```
Every transition is a guarded `UPDATE … WHERE status = <expected>`; `rowcount == 0` means someone else
already moved it, and the caller stops. That single rule is what makes callback-vs-reconciler races safe.

## Disbursement status

```
 (insert, UNIQUE payment_id) ──▶ initiating ──B2B accepted──▶ pending ──result 0──▶ sent
        lost the insert: stop        │                          │
                                     │ no/odd response          ├─result≠0──▶ failed  (auto-retry ≤ 3, spaced)
                                     ▼                          └─queue timeout──▶ failed/queue_timeout
                                  failed/api_error                                     (AMBIGUOUS)
        AMBIGUOUS = initiating | api_error | queue_timeout: never auto-retried, reported to a human.
```
`no_destination_configured` and explicit rejections are known not to have moved money and are retried
automatically. `oauth_unavailable` (no token, nothing sent) is likewise safe.

## Trust model for callbacks

1. Path secret (constant-time compare) is the only authentication Safaricom's callback offers.
2. Therefore a callback is a *hint*: success is accepted only if Safaricom's own STK query agrees and
   the amount equals what this payment asked for; failure is re-verified now and again on a schedule.
3. Every callback is stored first; outcomes are `applied | duplicate | unmatched | adopted | left_pending |
   recovered | ignored | orphan`. An unmatched SUCCESS is an alert, never a silent log line.

## Why payouts are claim-first

Sending B2B and *then* recording it lets two callers both send before either record hits the unique
constraint. Recording first (as `initiating`) makes the database arbitrate; the loser never calls Safaricom.
The cost is that a crash after the claim leaves an `initiating` row, which is exactly the ambiguous
state a human should look at.
