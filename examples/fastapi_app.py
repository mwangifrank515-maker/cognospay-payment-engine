"""A complete FastAPI integration: payment links, direct STK push, callbacks.

    pip install "cognospay-engine[fastapi]" uvicorn
    export MPESA_CONSUMER_KEY=... MPESA_CONSUMER_SECRET=... MPESA_PASSKEY=... \
           MPESA_CALLBACK_BASE_URL=https://<your public https URL> MPESA_CALLBACK_SECRET=<24+ random chars>
    uvicorn examples.fastapi_app:app

Safaricom must be able to reach MPESA_CALLBACK_BASE_URL (use a tunnel such as ngrok while developing).
"""
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cognospay_engine import (
    Base,
    DarajaClient,
    DarajaConfig,
    Hooks,
    Payment,
    PaymentEngine,
    PayoutRoute,
    mask_phone,
    production_problems,
)
from cognospay_engine.contrib.fastapi import create_callback_router, register_error_handlers

db_engine = create_engine(os.environ.get("DATABASE_URL", "sqlite:///./payments.db"),
                          connect_args={"check_same_thread": False} if "sqlite" in os.environ.get("DATABASE_URL", "sqlite") else {})
Base.metadata.create_all(db_engine)          # development only: use Alembic in production
SessionLocal = sessionmaker(bind=db_engine)


def get_db():
    with SessionLocal() as s:
        yield s


def build_engine():
    """Also the factory for the reconciler:  cognospay-engine reconcile --factory examples.fastapi_app:build_engine"""
    config = DarajaConfig.from_env()
    if os.environ.get("APP_ENV") == "production" and (problems := production_problems(config, payouts=False)):
        raise SystemExit("Refusing to start:\n - " + "\n - ".join(problems))
    eng = PaymentEngine(
        DarajaClient(config),
        # Remove payout_resolver for collect-only mode (money settles to your own shortcode).
        payout_resolver=lambda session, merchant_id: PayoutRoute("till", "555111", fee_fraction=0.02),
        hooks=Hooks(
            alert=lambda subject, msg: print("ALERT:", subject, msg),      # wire to SMS/email/Slack
            on_payment_succeeded=lambda session, p: print(f"paid: payment #{p.id} KES {p.amount}"),
        ),
    )
    return eng, SessionLocal


engine, _ = build_engine()
app = FastAPI(title="Payments")
app.include_router(create_callback_router(engine, get_db))      # POST /callback/{stk,b2b,b2b-timeout}/{secret}
register_error_handlers(app)                                    # EngineError -> JSON with the right status


def current_merchant_id(x_api_key: str = Header(...)) -> int:
    """Replace with YOUR authentication. merchant_id must never come from the request body."""
    if x_api_key != os.environ.get("DEMO_API_KEY", "dev-key"):
        raise HTTPException(status_code=401)
    return 1


class LinkIn(BaseModel):
    amount: int | None = None
    description: str | None = None


class StkIn(BaseModel):
    phone: str
    amount: int
    reference: str | None = None


class PayIn(BaseModel):
    phone: str
    amount: int | None = None


@app.post("/links")
def create_link(body: LinkIn, db=Depends(get_db), merchant_id: int = Depends(current_merchant_id)):
    return engine.create_link(db, merchant_id, amount=body.amount, description=body.description)


@app.post("/pay/{link_id}")                  # public: the payer's phone number is the only input
def pay(link_id: str, body: PayIn, db=Depends(get_db)):
    return {"payment_id": engine.pay_link(db, link_id, body.phone, body.amount).payment_id}


@app.post("/stk")
def stk(body: StkIn, db=Depends(get_db), merchant_id: int = Depends(current_merchant_id)):
    return {"payment_id": engine.initiate_stk(db, merchant_id, body.phone, body.amount, body.reference).payment_id}


@app.get("/payments/{payment_id}")
def payment_status(payment_id: int, db=Depends(get_db), merchant_id: int = Depends(current_merchant_id)):
    p = db.get(Payment, payment_id)
    if not p or p.merchant_id != merchant_id:
        raise HTTPException(status_code=404)       # same answer for "missing" and "not yours"
    return {"id": p.id, "status": p.status, "amount": p.amount, "phone": mask_phone(p.phone),
            "receipt": p.mpesa_receipt}
