import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cognospay_engine import Base, Hooks, Payment, PaymentEngine, PaymentLink, PayoutRoute
from cognospay_engine.clock import utcnow
from cognospay_engine.testing import FakeGateway, stk_callback_body

SECRET = "pytest-callback-secret-0123456789"


@pytest.fixture()
def sf():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)


@pytest.fixture()
def db(sf):
    with sf() as s:
        yield s


@pytest.fixture()
def gw():
    return FakeGateway()


@pytest.fixture()
def alerts():
    return []


@pytest.fixture()
def events():
    return {"succeeded": [], "failed": []}


@pytest.fixture()
def eng(gw, alerts, events):
    hooks = Hooks(alert=lambda s, m: alerts.append((s, m)),
                  on_payment_succeeded=lambda sess, p: events["succeeded"].append(p.id),
                  on_payment_failed=lambda sess, p: events["failed"].append(p.id))
    return PaymentEngine(gw, hooks=hooks, callback_secret=SECRET,
                         payout_resolver=lambda sess, mid: PayoutRoute("till", "555111"))


# ── factories ────────────────────────────────────────────────────────────────
def make_link(db, amount=100, max_uses=1, phone=None):
    link = PaymentLink(link_id=str(uuid.uuid4()), token=str(uuid.uuid4()), merchant_id=1, amount=amount,
                       description="t", phone=phone, channel="Link", max_uses=max_uses, used_count=0,
                       expires_at=utcnow() + timedelta(hours=1), created_at=utcnow())
    db.add(link); db.commit()
    return link.link_id


def make_payment(db, amount=100, status="pending", cid=None, age_min=0, link_id=None, phone="254712345678"):
    cid = cid or f"ws_CO_{uuid.uuid4().hex[:12]}"
    p = Payment(merchant_id=1, amount=amount, phone=phone, status=status, checkout_request_id=cid,
                link_id=link_id, channel="Link", created_at=utcnow() - timedelta(minutes=age_min))
    db.add(p); db.commit()
    return p.id, cid


def stk_body(cid, amount=100, code=0, receipt="RCP123", phone=None):
    return stk_callback_body(cid, amount, code, receipt, phone, desc="x")


def pay(db, pid):
    from sqlalchemy import select as _s
    return db.execute(_s(Payment).where(Payment.id == pid).execution_options(populate_existing=True)).scalar_one()


def disb(db, pid):
    from cognospay_engine import Disbursement
    return db.execute(select(Disbursement).where(Disbursement.payment_id == pid)
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def inbox(db, kind=None):
    from cognospay_engine import MpesaCallback
    q = select(MpesaCallback).order_by(MpesaCallback.id)
    if kind:
        q = q.where(MpesaCallback.kind == kind)
    return db.execute(q.execution_options(populate_existing=True)).scalars().all()
