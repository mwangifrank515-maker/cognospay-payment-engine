# Integrating cognospay-engine

The engine owns money safety (verification, idempotency, payouts, reconciliation). **You** own your
users, orders, auth and UI. Integration is five small pieces.

```
 your checkout ──initiate_stk(reference=order_id)──▶ engine ──STK push──▶ customer's phone
                                                       ▲                         │ PIN
   your browser polls /checkout/<id> ◀── your order ◀──┤ hook: on_payment_succeeded
                                                       └── verified callback ◀── Safaricom
```

## 0. Decide your mode

| You want to… | Mode |
|---|---|
| Get paid into **your own** paybill/till | **Collect-only**: no `payout_resolver`, no initiator/security credential needed |
| Collect for **many merchants** and pay them out (minus your fee) | **With payouts**: provide a `payout_resolver` |

## 1. Prerequisites

- A Daraja app (consumer key/secret), a shortcode and its passkey. Develop on the sandbox first.
- A **public https URL** Safaricom can reach (`localhost` callbacks never arrive; use a tunnel while developing).
- A database SQLAlchemy supports. **PostgreSQL for production**; SQLite is for development and tests.

## 2. Install and create the tables

```bash
pip install git+https://github.com/mwangifrank515-maker/cognospay-engine.git@v0.1.0
```
Development: `Base.metadata.create_all(db_engine)`. Production: give Alembic the engine's metadata so the tables
live in your normal migrations:

```python
# alembic/env.py
from cognospay_engine import Base as EngineBase
target_metadata = [YourBase.metadata, EngineBase.metadata]
```
The four tables are `payments`, `payment_links`, `disbursements`, `mpesa_callbacks`. If you already have tables
with those names, review `models.py` first.

## 3. Build ONE engine at startup

```python
from cognospay_engine import (DarajaClient, DarajaConfig, Hooks, PaymentEngine, production_problems)

config = DarajaConfig.from_env()                      # MPESA_* variables, see the README
if os.environ["APP_ENV"] == "production":
    problems = production_problems(config, payouts=False)
    if problems: raise SystemExit("\n".join(problems))   # sandbox URL, test shortcode, weak secret, http callback...

engine = PaymentEngine(
    DarajaClient(config),
    hooks=Hooks(on_payment_succeeded=mark_order_paid,   # section 5
                alert=notify_ops),                      # SMS/email/Slack: something a human reads
)
```
It is thread-safe to share (the Daraja token cache is locked). Sessions are **not** shared: open one per
request/task and pass it in.

## 4. Mount the three callback endpoints

Safaricom posts results to `…/callback/stk/<secret>`, `…/callback/b2b/<secret>`, `…/callback/b2b-timeout/<secret>`.

- **FastAPI:** `app.include_router(create_callback_router(engine, get_db))` (`examples/fastapi_app.py`).
- **Flask:** three small views (`examples/flask_app.py`, tested).
- **Anything else (Django, aiohttp, Starlette…):** verify `engine.verify_callback_secret(secret)` (404 if false),
  parse the JSON, call `engine.handle_stk_callback(session, body)` / `handle_b2b_callback(session, body, kind)`,
  and answer `{"ResultCode": 0, "ResultDesc": "Accepted"}` **even for malformed bodies** (or Safaricom retries
  forever). In async frameworks run the call in a worker thread; it makes blocking HTTP requests.

Call `install_callback_redaction()` so access logs never print the secret in the URL.

## 5. Start payments and react to them

Start a payment from **your** authenticated endpoint. Tie it to your order with `reference`:

```python
r = engine.initiate_stk(session, merchant_id=current_merchant.id, phone=body.phone,
                        amount=order.total, reference=order.id)
return {"payment_id": r.payment_id}
```
Never take `merchant_id` from the request body; take it from your auth. Errors raise `EngineError` subclasses
carrying `http_status` + a user-safe `message` (invalid phone, throttled, STK rejected...). Map them in one handler.

Payment **links** (a shareable URL/QR) instead: `engine.create_link(...)`, then your public page calls
`engine.pay_link(session, link_id, phone)`.

React to success in the hook. It runs **once**, after SUCCESS is committed:

```python
def mark_order_paid(session, payment):
    session.execute(update(Order).where(Order.id == payment.reference).values(status="paid",
                    receipt=payment.mpesa_receipt))
    session.commit()
```

**The browser must never be told "paid" by the browser.** The front end starts the push, then polls *your* endpoint
(which reads your order or `payments.status`) every 2–3 s for about a minute. The only source of truth is the
server-side callback or the reconciler.

> Hooks run after the commit. If your process dies in between, the hook won't re-run. Keep your state derivable
> from `payments.status = 'SUCCESS'`, or add a `Hooks.maintenance` job that re-syncs.

## 6. Run the reconciler

It settles everything the happy path can leave in limbo (lost callbacks, crashes between "paid" and "payout").
**Not optional in production.**

```bash
cognospay-engine reconcile --factory yourapp.payments:build_engine --loop 300   # build_engine() -> (engine, SessionLocal)
```
Run it as a second process/container, or call `engine.reconcile(session)` from your scheduler every few minutes.
Set `EngineSettings(heartbeat_file=...)` or `heartbeat_ping_url` so you notice when it stops.

## 7. Pay merchants out (optional)

```python
def resolver(session, merchant_id):                    # read YOUR merchants table
    m = session.get(Merchant, merchant_id)
    return PayoutRoute("till", m.till, fee_fraction=0.02) if m.till else None   # None = nowhere to pay yet

engine = PaymentEngine(DarajaClient(config), payout_resolver=resolver, hooks=...)
```
After a verified payment the engine claims a payout row first, then sends B2B for `gross − fee`. Needs
`MPESA_INITIATOR_NAME` and `MPESA_SECURITY_CREDENTIAL`. **Ambiguous** payouts (timeouts) are reported, never retried.

## 8. Test without Safaricom

```python
from cognospay_engine.testing import FakeGateway, stk_callback_body
engine.gateway = FakeGateway()                      # records every call; script its answers
engine.handle_stk_callback(session, stk_callback_body(checkout_id, amount=500))
```
Also run `python examples/flet_dashboard/app.py` to *see* every failure mode, then one real sandbox payment end to end.

## Go-live checklist

- [ ] `production_problems(config)` returns `[]`
- [ ] Callback URL reachable from the internet; registered/used exactly as built (`DarajaConfig.callback_base_url`)
- [ ] PostgreSQL; tables migrated through Alembic; index on `payments.checkout_request_id`
- [ ] Reconciler running and monitored; `Hooks.alert` goes to a human
- [ ] `merchant_id` comes from your auth; rate-limit your public pay endpoints per IP (the engine throttles per phone)
- [ ] Retention/encryption plan for phone numbers in `payments` and `mpesa_callbacks.payload`
- [ ] One real sandbox payment, one duplicate-callback replay, one forced timeout

## Not using Python?

The engine is a Python library. A PHP/Node/Laravel/Java backend can run it as a small **separate service** (the
FastAPI example is the starting point: `/stk`, `/links`, `/pay/<link>`, `/payments/<id>`) and call it over HTTP.
Two pieces that service doesn't have yet: **API-key auth** for your other systems, and **outbound webhooks** to
tell them a payment succeeded (today that is the `on_payment_succeeded` hook inside the Python process).
See the roadmap in the README.
