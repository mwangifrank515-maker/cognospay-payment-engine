"""Starting payments: payment links, direct STK push, per-phone throttle."""
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, text, update

from .clock import utcnow
from .errors import (
    EngineError,
    InvalidPhone,
    LinkExhausted,
    LinkExpired,
    LinkNotAutoChargeable,
    LinkNotFound,
    PaymentStartFailed,
    PhoneThrottled,
    StkRejected,
    StkUnconfirmed,
)
from .fees import require_whole_shillings
from .models import Payment, PaymentLink, PaymentStatus
from .phone import mask_phone, normalize_msisdn

logger = logging.getLogger("cognospay_engine.payments")


@dataclass(frozen=True)
class PaymentStarted:
    """Deliberately minimal. The final outcome only ever comes from the secured server-to-server
    callback, never from anything a client tells you. Don't hand the CheckoutRequestID to an
    anonymous browser."""
    payment_id: int


# ── links ───────────────────────────────────────────────────────────────────
def create_link(eng, session, merchant_id, *, amount=None, phone=None, description=None,
                max_uses=1, expires_in_hours=24, channel="Link") -> dict:
    """``amount=None`` makes an open-amount link (the payer types it). ``phone`` pins the link to
    one number for auto-charge. ``merchant_id`` must come from YOUR authentication, never from
    request input."""
    if channel not in ("Link", "QR"):
        raise ValueError('channel must be "Link" or "QR"')
    if amount is not None:
        amount = float(require_whole_shillings(amount))
    if phone is not None and str(phone).strip():
        phone = normalize_msisdn(phone)
    else:
        phone = None
    link = PaymentLink(
        link_id=str(uuid.uuid4()), token=str(uuid.uuid4()), merchant_id=merchant_id, amount=amount,
        description=description, phone=phone, channel=channel, max_uses=max(1, int(max_uses)),
        used_count=0, expires_at=utcnow() + timedelta(hours=max(1, int(expires_in_hours))),
        created_at=utcnow())
    session.add(link)
    session.commit()
    return {"link_id": link.link_id, "token": link.token, "expires_at": link.expires_at.isoformat()}


def get_valid_link(session, *, link_id=None, token=None) -> PaymentLink:
    """Fetch a link and enforce expiry + usage limits."""
    if link_id:
        q = select(PaymentLink).where(PaymentLink.link_id == link_id)
    elif token:
        q = select(PaymentLink).where(PaymentLink.token == token)
    else:
        raise LinkNotFound()
    link = session.execute(q.execution_options(populate_existing=True)).scalar_one_or_none()
    if not link:
        raise LinkNotFound()
    if link.expires_at and utcnow() > link.expires_at:
        raise LinkExpired()
    if (link.used_count or 0) >= (link.max_uses or 1):
        raise LinkExhausted()
    return link


# ── throttle ────────────────────────────────────────────────────────────────
def check_phone_throttle(eng, session, phone: str) -> None:
    """Block a phone from being hit with too many STK pushes in a short window, whatever link
    or IP the requests came through."""
    # Check-then-insert is racy: N parallel requests could all read "0 recent" and all proceed.
    # On Postgres take a transaction-scoped advisory lock keyed on the phone so concurrent
    # attempts for the SAME number queue up (released at commit/rollback).
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:p))"), {"p": phone})
    s = eng.settings
    cutoff = utcnow() - timedelta(minutes=s.phone_throttle_window_minutes)
    count = session.execute(select(func.count()).select_from(Payment).where(
        Payment.phone == phone, Payment.created_at > cutoff)).scalar() or 0
    if count >= s.phone_throttle_limit:
        session.rollback()    # release the advisory lock
        logger.warning("phone throttle hit for %s (%s recent attempts)", mask_phone(phone), count)
        raise PhoneThrottled()


# ── pushing ─────────────────────────────────────────────────────────────────
def _mark_unconfirmed(eng, session, payment_id: int, reason: str) -> None:
    """The STK request MAY have reached Safaricom, so the customer may still get a prompt and
    pay. Do NOT mark FAILED and do NOT release a link use: keep the row so a later SUCCESS
    callback can be matched to it (see callbacks.adopt_unconfirmed)."""
    try:
        session.execute(update(Payment).where(Payment.id == payment_id,
                                              Payment.status == PaymentStatus.PENDING)
                        .values(status=PaymentStatus.UNCONFIRMED, result_desc=f"stk_unknown:{reason}"[:200]))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("could not mark payment_id=%s unconfirmed", payment_id)
    eng.alert(f"STK push #{payment_id} outcome unknown",
              f"payment_id={payment_id} reason={reason}: the request may have reached Safaricom. "
              "Watch for an unmatched SUCCESS callback.")


def _push(eng, session, payment_id: int, phone: str, amount: int, account_ref: str, link=None):
    resp = eng.gateway.stk_push(phone=phone, amount=amount, account_ref=account_ref,
                                transaction_desc=eng.settings.stk_description)
    if resp and resp.get("_unknown"):
        _mark_unconfirmed(eng, session, payment_id, str(resp.get("reason")))
        raise StkUnconfirmed()

    if not resp or not resp.get("CheckoutRequestID"):
        try:
            session.execute(update(Payment).where(Payment.id == payment_id)
                            .values(status=PaymentStatus.FAILED))
            if link is not None:
                # The payer was never charged: don't burn their link on a transient M-Pesa error.
                session.execute(update(PaymentLink).where(PaymentLink.id == link.id,
                                                          PaymentLink.used_count > 0)
                                .values(used_count=PaymentLink.used_count - 1))
            session.commit()
        except Exception:
            session.rollback()
        raise StkRejected()

    try:
        session.execute(update(Payment).where(Payment.id == payment_id)
                        .values(checkout_request_id=resp["CheckoutRequestID"]))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("failed to record checkout_request_id for payment_id=%s", payment_id)
    return PaymentStarted(payment_id=payment_id)


def _start_link_payment(eng, session, link, phone, amount):
    phone = normalize_msisdn(phone)
    amount = require_whole_shillings(amount)
    check_phone_throttle(eng, session, phone)
    try:
        # Claim one use ATOMICALLY: the pre-check in get_valid_link is only a fast path. This
        # guarded UPDATE lets exactly max_uses callers through, whatever the concurrency.
        claim = session.execute(update(PaymentLink).where(
            PaymentLink.id == link.id, PaymentLink.used_count < PaymentLink.max_uses)
            .values(used_count=PaymentLink.used_count + 1))
        if claim.rowcount == 0:
            session.rollback()
            raise LinkExhausted()
        p = Payment(merchant_id=link.merchant_id, amount=float(amount), phone=phone,
                    status=PaymentStatus.PENDING, link_id=link.link_id, channel=link.channel,
                    created_at=utcnow())
        session.add(p)
        session.flush()
        payment_id = p.id
        session.commit()
    except EngineError:
        raise
    except Exception:
        session.rollback()
        logger.exception("failed to create pending payment for link_id=%s", link.link_id)
        raise PaymentStartFailed() from None
    return _push(eng, session, payment_id, phone, amount, str(payment_id), link=link)


def pay_link(eng, session, link_id, phone, amount=None) -> PaymentStarted:
    """Public payer flow: STK push for a payment link. A fixed-amount link ALWAYS uses the
    server-stored amount, never whatever the client sends."""
    if not phone:
        raise InvalidPhone("Phone number is required")
    link = get_valid_link(session, link_id=link_id)
    amt = link.amount if link.amount is not None else amount
    return _start_link_payment(eng, session, link, phone, amt)


def auto_charge_link(eng, session, token) -> PaymentStarted:
    """Charge a link that has both a fixed amount and a pinned phone. Show a confirmation page
    first: link pre-fetchers (chat apps, antivirus) must never trigger the charge."""
    link = get_valid_link(session, token=token)
    if not link.phone:
        raise LinkNotAutoChargeable("This link has no phone number on file")
    if link.amount is None:
        raise LinkNotAutoChargeable("This link has no fixed amount to auto-charge")
    return _start_link_payment(eng, session, link, link.phone, link.amount)


def initiate_stk(eng, session, merchant_id, phone, amount, reference=None,
                 created_by_user_id=None) -> PaymentStarted:
    """Merchant pushes straight to a phone, no link involved. Authenticate the caller first."""
    phone = normalize_msisdn(phone)
    amount = require_whole_shillings(amount)
    check_phone_throttle(eng, session, phone)
    try:
        p = Payment(merchant_id=merchant_id, amount=float(amount), phone=phone,
                    status=PaymentStatus.PENDING, reference=reference, channel="STK",
                    created_by_user_id=created_by_user_id, created_at=utcnow())
        session.add(p)
        session.flush()
        payment_id = p.id
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("failed to create direct STK payment for merchant_id=%s", merchant_id)
        raise PaymentStartFailed() from None
    return _push(eng, session, payment_id, phone, amount, reference or str(payment_id))
