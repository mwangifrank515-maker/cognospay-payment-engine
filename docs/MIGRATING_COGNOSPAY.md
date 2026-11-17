# Moving CognosPay onto the engine

The engine's tables and column names match CognosPay's `payments`, `payment_links`, `disbursements`
and `mpesa_callbacks`, so **no data migration is needed**. Env var names are unchanged, so the callback
URLs already registered with Safaricom keep working.

## What replaces what

| CognosPay today | After |
|---|---|
| `app/mpesa.py` | `cognospay_engine.DarajaClient` (config from the same `MPESA_*` env vars) |
| `app/routes/callback.py` (callbacks, `_disburse`, inbox) | `create_callback_router(engine, get_db)` |
| `app/routes/payments.py` (`_create_pending_payment_and_push`, links, throttle, direct STK) | `engine.pay_link / auto_charge_link / initiate_stk / create_link` behind your existing auth + rate limits + HTML pages |
| `app/services/fees.py`, `app/utils/phone.py`, `app/core/log_redaction.py` | `cognospay_engine.fees / phone / redaction` |
| `app/services/reconciliation.py` + `scripts/run_reconciliation.py` | `cognospay-engine reconcile --factory app.payments_engine:build_engine --loop 300` |
| `app/utils/alerts.py` | stays; passed as `Hooks(alert=alert_admin)` |
| School fee / invoice side-effects inside `finalize_successful_payment` | `Hooks(on_payment_succeeded=...)` below |

## The adapter (new file `app/payments_engine.py`)

```python
from sqlalchemy import text
from cognospay_engine import DarajaClient, DarajaConfig, EngineSettings, Hooks, PaymentEngine, PayoutRoute
from app.database import SessionLocal
from app.utils.alerts import alert_admin
from app.utils.clock import utcnow


def payout_resolver(db, merchant_id):
    row = db.execute(text("SELECT till_number, paybill_number, billing_type, commission_pct "
                          "FROM merchants WHERE id = :m"), {"m": merchant_id}).fetchone()
    if not row:
        return None
    fraction = 0.0 if row.billing_type == "flat_monthly" else row.commission_pct   # None -> engine default (2%)
    if row.till_number:                    # Till preferred over Paybill, as before
        return PayoutRoute("till", row.till_number, fraction)
    if row.paybill_number:
        return PayoutRoute("paybill", row.paybill_number, fraction)
    return None


def on_payment_succeeded(db, p):
    """Everything finalize_successful_payment() used to do between SUCCESS and the payout."""
    from app.services.fee_payments import apply_link_payment, apply_stk_to_invoice
    from app.services.fee_notifications import notify_fee_payment      # move _notify_fee_payment here
    receipt = p.mpesa_receipt or ""
    if p.link_id:
        db.execute(text("UPDATE invoices SET status='paid', paid_at=:t, mpesa_receipt=:r "
                        "WHERE payment_link_id=:l AND status != 'paid' AND student_id IS NULL"),
                   {"t": utcnow().isoformat(sep=" "), "r": p.mpesa_receipt, "l": p.link_id})
        db.commit()
        apply_link_payment(db, p.merchant_id, p.link_id, float(p.amount), receipt)
        notify_fee_payment(db, p.link_id, receipt)
    else:
        apply_stk_to_invoice(db, p.merchant_id, p.reference or "", float(p.amount), receipt)


def platform_billing_job(db, now):
    from app.services.platform_billing import generate_monthly_invoices
    return len(generate_monthly_invoices(db, "system"))


def data_rights_job(db, now):
    from app.services.data_rights import run_data_rights_maintenance
    return run_data_rights_maintenance(db, now)


def build_engine():
    settings = EngineSettings(heartbeat_file="/tmp/reconciler_ok")      # same marker the docker healthcheck reads
    hooks = Hooks(alert=alert_admin, on_payment_succeeded=on_payment_succeeded,
                  maintenance=[platform_billing_job, data_rights_job])
    return PaymentEngine(DarajaClient(DarajaConfig.from_env()), payout_resolver=payout_resolver,
                         hooks=hooks, settings=settings), SessionLocal


engine, _ = build_engine()
```
The Redis ping + Redis heartbeat key that `run_all` used to do are not in the engine: put them in a
`maintenance` job or `Hooks.heartbeat`.

## Staged rollout (each step is independently reversible)

1. `pip install` the pinned tag. Add `app/payments_engine.py`. No behaviour change yet.
2. Swap **only the callback router** (smallest surface, same URLs). Run the sandbox flow; compare `mpesa_callbacks.outcome` values.
3. Swap the start-payment routes. Keep your `slowapi` limits, permissions (`require_permission`) and HTML pages; they call `engine.*`. Map errors with `register_error_handlers` (note `open_payment_link` renders `e.detail`, which is now `e.message`).
4. Swap the reconciler container to the CLI command above.
5. After ~2 weeks of clean `find_needs_attention` output, delete the old modules.

## Things to know

- **Tests:** CognosPay's tests monkeypatch `app.routes.payments.mpesa_stk_push`, `app.routes.callback.query_transaction`, etc. Those targets disappear. Inject `cognospay_engine.testing.FakeGateway` as `engine.gateway` instead (`tests/conftest.py` in this repo shows the shape).
- **Models:** keep CognosPay's own `Payment`/`Disbursement` ORM classes for dashboards and reports; the engine maps the same tables through its own `Base`. CognosPay's Alembic metadata is unaffected. The engine defines no FK to `merchants`; the database keeps the one it already has.
- **Recommended index** (every callback looks up by it; CognosPay's model doesn't index it today):
  `CREATE INDEX IF NOT EXISTS ix_payments_checkout_request_id ON payments (checkout_request_id);`
- **Reconciler summary shape** changed slightly (no `redis_ok`, `platform_invoices_generated`, `data_rights` keys; maintenance results appear as `maintenance_<name>`).
- **Behaviour differences** are listed in CHANGELOG.md; the two that touch Daraja requests are the `AccountReference` (12) / `TransactionDesc` (13) truncation and the User-Agent header now sent on every call. Confirm both in the sandbox first.
