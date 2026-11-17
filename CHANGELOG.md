# Changelog

## 0.1.0

Initial extraction from CognosPay's payment code (STK push, links, callbacks, B2B payouts, reconciliation).

### Decoupled
- No global settings, no framework: `DarajaConfig`, `EngineSettings`, `PaymentEngine(gateway, payout_resolver, hooks)`.
- Raw SQL against `merchants`/`invoices`/`students` replaced by `payout_resolver` and hooks; school-fee and
  invoice side-effects, Redis checks, platform billing and data-rights jobs are no longer part of the engine.
- Raw `text()` SQL ported to SQLAlchemy 2.0; guarded-UPDATE and claim-first semantics preserved and covered by tests.
- Optional collect-only mode (no `payout_resolver` = no B2B).

### Behaviour changes vs CognosPay (review each)
- `AccountReference` is truncated to 12 chars and `TransactionDesc` to 13 (documented Daraja limits). Previously unbounded.
- The browser-style `User-Agent` header is sent on every Daraja call (previously STK push only).
- Phone numbers are masked in all log lines; malformed callback bodies are no longer logged in full.
- A throttled request (429) now rolls back its transaction, releasing the Postgres advisory lock immediately.
- All FAILED transitions go through one guarded `mark_failed()` and fire the new `on_payment_failed` hook.
  Synchronous STK rejections still raise to the caller and do **not** fire it.
- `payments.token` and `payments.transaction_id` (unused) are not mapped; `merchant_id` has no FK.
- `ix_payments_checkout_request_id` and `ix_payments_link_id` are declared (existing databases: add via migration).
- `finalize_successful_payment(engine, session, payment_id, receipt)` loads the payment itself (was passed merchant/amount/link).

### Added
- `examples/flet_dashboard`: interactive Flet 0.86 "Engine Lab" (12 scenarios, live pipeline, tables, scriptable fake Safaricom); `demo` extra.
- `FakeGateway`, `stk_callback_body`, `b2b_result_body` test helpers; FastAPI adapter; `cognospay-engine` CLI;
  `production_problems()` startup check; `DarajaConfig.stk_transaction_type` / `party_b` (default unchanged; untested against live Daraja).
