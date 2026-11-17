"""The same integration in Flask (sync, no extra adapter needed).

    pip install -e . flask
    export MPESA_CONSUMER_KEY=... MPESA_CONSUMER_SECRET=... MPESA_PASSKEY=... \
           MPESA_CALLBACK_BASE_URL=https://<public https url> MPESA_CALLBACK_SECRET=<24+ random chars>
    flask --app examples.flask_app run
"""
import os

from flask import Flask, abort, jsonify, request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cognospay_engine import (
    Base,
    DarajaClient,
    DarajaConfig,
    EngineError,
    Hooks,
    Payment,
    PaymentEngine,
    install_callback_redaction,
    mask_phone,
)

db = create_engine(os.environ.get("DATABASE_URL", "sqlite:///./flask_payments.db"))
Base.metadata.create_all(db)                       # development only: use Alembic in production
SessionLocal = sessionmaker(bind=db)
install_callback_redaction()

ORDERS: dict[str, str] = {}                        # stand-in for YOUR orders table: reference -> status


def mark_order_paid(session, payment):
    ORDERS[payment.reference] = "paid"             # your real code: UPDATE orders SET status='paid' ...


engine = PaymentEngine(DarajaClient(DarajaConfig.from_env()),     # collect-only: no payout_resolver
                       hooks=Hooks(on_payment_succeeded=mark_order_paid,
                                   alert=lambda subject, msg: print("ALERT:", subject, msg)))
app = Flask(__name__)


@app.errorhandler(EngineError)
def engine_error(e):
    return jsonify(detail=e.message), e.http_status


@app.post("/checkout")
def checkout():
    body = request.get_json()
    ORDERS[body["order_id"]] = "awaiting_payment"
    with SessionLocal() as s:
        r = engine.initiate_stk(s, merchant_id=1, phone=body["phone"], amount=body["amount"],
                                reference=body["order_id"])         # the reference ties the payment to YOUR order
    return {"payment_id": r.payment_id}


@app.get("/checkout/<int:payment_id>")                               # the browser polls this
def checkout_status(payment_id):
    with SessionLocal() as s:
        p = s.get(Payment, payment_id)
        if not p:
            abort(404)
        return {"status": p.status, "phone": mask_phone(p.phone), "order": ORDERS.get(p.reference)}


@app.post("/callback/stk/<secret>")
def stk_callback(secret):
    if not engine.verify_callback_secret(secret):
        abort(404)
    body = request.get_json(silent=True)
    if body is not None:
        with SessionLocal() as s:
            engine.handle_stk_callback(s, body)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}               # always accept, or Safaricom retries


@app.post("/callback/b2b/<secret>")
def b2b_callback(secret):
    if not engine.verify_callback_secret(secret):
        abort(404)
    body = request.get_json(silent=True)
    if body is not None:
        with SessionLocal() as s:
            engine.handle_b2b_callback(s, body, "b2b")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/callback/b2b-timeout/<secret>")
def b2b_timeout_callback(secret):
    if not engine.verify_callback_secret(secret):
        abort(404)
    body = request.get_json(silent=True)
    if body is not None:
        with SessionLocal() as s:
            engine.handle_b2b_callback(s, body, "b2b_timeout")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
