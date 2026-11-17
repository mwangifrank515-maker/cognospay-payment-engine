"""Processing Safaricom callbacks (STK result, B2B result, B2B queue timeout).

Trust model: the secret in the callback URL is the only thing proving who called, so a callback
is never trusted on its own. A claimed SUCCESS is cross-checked with Safaricom's STK query and
the amount; a claimed FAILURE is re-checked too (and re-checked again later by the reconciler).
"""
import logging
from datetime import timedelta

from sqlalchemy import select, update

from .clock import utcnow
from .constants import DELIBERATE_FAIL_DESCS
from .fees import amounts_match
from .inbox import apply_b2b, disbursement_exists, parse_b2b_result, record_callback, set_outcome
from .models import Payment, PaymentStatus
from .payouts import disburse
from .phone import mask_phone

logger = logging.getLogger("cognospay_engine.callbacks")


def _fetch(session, payment_id: int):
    return session.execute(select(Payment).where(Payment.id == payment_id)
                           .execution_options(populate_existing=True)).scalar_one_or_none()


def _transition(session, payment_id: int, expect: str, **values) -> int:
    """Guarded UPDATE: only moves the payment if it is still in ``expect``. This is what stops a
    callback and the reconciler from both crediting/paying the same payment."""
    res = session.execute(update(Payment).where(Payment.id == payment_id, Payment.status == expect)
                          .values(**values).execution_options(synchronize_session=False))
    session.commit()
    return res.rowcount or 0


def mark_failed(eng, session, payment_id: int, reason: str, *, expect: str = PaymentStatus.PENDING) -> bool:
    if not _transition(session, payment_id, expect, status=PaymentStatus.FAILED, result_desc=reason[:200]):
        return False
    eng.fire(session, "on_payment_failed", _fetch(session, payment_id))
    return True


def _flag_failed(eng, session, row, checkout_id: str, reason: str, detail: str) -> None:
    """Mark a pending payment FAILED for manual review and alert a human."""
    mark_failed(eng, session, row.id, reason)
    eng.alert(f"Payment #{row.id} {reason} — needs manual reconciliation",
              f"checkout_id={checkout_id} merchant_id={row.merchant_id} payment_id={row.id} {detail}")


def finalize_successful_payment(eng, session, payment_id: int, receipt: str | None) -> bool:
    """Everything that follows a CONFIRMED payment: mark SUCCESS, run your hook, pay out.

    Returns False (and does nothing) if the payment was no longer 'pending', i.e. someone else
    already finalized it. Shared by the Safaricom callback and the reconciler so both behave
    identically."""
    if not _transition(session, payment_id, PaymentStatus.PENDING,
                       status=PaymentStatus.SUCCESS, mpesa_receipt=receipt):
        return False
    logger.info("payment SUCCESS payment_id=%s", payment_id)
    payment = _fetch(session, payment_id)
    eng.fire(session, "on_payment_succeeded", payment)     # never blocks the payout
    try:
        disburse(eng, session, payment_id, payment.merchant_id, float(payment.amount))
    except Exception:
        session.rollback()
        logger.exception("disbursement failed for payment_id=%s — will need manual processing", payment_id)
    return True


def _safe(data: dict) -> dict:
    d = dict(data)
    if d.get("PhoneNumber") is not None:
        d["PhoneNumber"] = mask_phone(str(d["PhoneNumber"]))
    return d


def adopt_unconfirmed(eng, session, checkout_id: str, data: dict):
    """A SUCCESS callback for a CheckoutRequestID we never stored. If exactly one 'unconfirmed'
    payment (STK request whose answer was lost) matches phone + amount within the adopt window,
    attach the id to it and let the normal verification path (which re-asks Safaricom) decide."""
    phone, amount = str(data.get("PhoneNumber") or ""), data.get("Amount")
    if not phone or amount is None:
        return None
    cutoff = utcnow() - timedelta(minutes=eng.settings.adopt_window_minutes)
    cands = [p for p in session.execute(select(Payment).where(
        Payment.status == PaymentStatus.UNCONFIRMED, Payment.checkout_request_id.is_(None),
        Payment.phone == phone, Payment.created_at > cutoff)).scalars().all()
        if abs(float(p.amount) - float(amount)) < 0.005]
    if len(cands) != 1:
        return None
    pid = cands[0].id
    won = session.execute(update(Payment).where(
        Payment.id == pid, Payment.status == PaymentStatus.UNCONFIRMED,
        Payment.checkout_request_id.is_(None))
        .values(checkout_request_id=checkout_id, status=PaymentStatus.PENDING,
                result_desc="adopted_from_callback").execution_options(synchronize_session=False))
    session.commit()
    return _fetch(session, pid) if won.rowcount else None


def _backfill_receipt(session, row, receipt) -> None:
    """The reconciler settles payments without a receipt (the STK query carries none). When the
    real callback turns up later, fill it in."""
    if not receipt:
        return
    session.execute(update(Payment).where(
        Payment.id == row.id, Payment.status == PaymentStatus.SUCCESS,
        (Payment.mpesa_receipt.is_(None)) | (Payment.mpesa_receipt == ""))
        .values(mpesa_receipt=receipt).execution_options(synchronize_session=False))
    session.commit()


def process_stk_callback(eng, session, body: dict) -> None:
    """All the (blocking) work for one STK callback. Run it in a worker thread from async code."""
    try:
        stk = body["Body"]["stkCallback"]
        checkout_request_id = stk["CheckoutRequestID"]
        claimed_code = stk["ResultCode"]
        result_desc = stk.get("ResultDesc")
    except (KeyError, TypeError, AttributeError):
        logger.warning("callback rejected: malformed body")
        record_callback(session, "stk", None, None, body)
        return

    logger.info("callback received: checkout_id=%s claimed_code=%s", checkout_request_id, claimed_code)
    cb_id = record_callback(session, "stk", checkout_request_id, claimed_code, body)
    claimed_ok = str(claimed_code) == "0"
    q = eng.gateway.query_transaction

    try:
        items = (stk.get("CallbackMetadata") or {}).get("Item", [])
        data = {i.get("Name"): i.get("Value") for i in items if isinstance(i, dict)}

        row = session.execute(select(Payment).where(Payment.checkout_request_id == checkout_request_id)
                              .execution_options(populate_existing=True)).scalars().first()

        if row is None:
            if claimed_ok:
                row = adopt_unconfirmed(eng, session, checkout_request_id, data)
                if row is not None:
                    logger.warning("adopted callback %s for unconfirmed payment_id=%s",
                                   checkout_request_id, row.id)
                    set_outcome(session, cb_id, "adopted", processed=False)
            if row is None:
                set_outcome(session, cb_id, "unmatched")
                if claimed_ok:
                    logger.error("SUCCESS callback matches no payment: checkout_id=%s data=%s",
                                 checkout_request_id, _safe(data))
                    eng.alert(
                        f"Unmatched M-Pesa SUCCESS callback {data.get('MpesaReceiptNumber') or checkout_request_id}",
                        f"checkout_id={checkout_request_id} amount={data.get('Amount')} "
                        f"phone={mask_phone(str(data.get('PhoneNumber') or ''))} — a customer paid "
                        "but no payment row exists. Match it by hand.")
                return

        # A late SUCCESS after we recorded FAILED (lost/forged failure callback, timeout code
        # followed by a real payment): re-open it; the verification below decides.
        if (row.status == PaymentStatus.FAILED and claimed_ok
                and (row.result_desc or "") not in DELIBERATE_FAIL_DESCS):
            if _transition(session, row.id, PaymentStatus.FAILED, status=PaymentStatus.PENDING):
                logger.warning("re-opened FAILED payment_id=%s on a SUCCESS callback", row.id)
                row = _fetch(session, row.id)

        if row.status == PaymentStatus.SUCCESS:
            if claimed_ok:
                _backfill_receipt(session, row, data.get("MpesaReceiptNumber"))
            set_outcome(session, cb_id, "duplicate")
            return
        if row.status != PaymentStatus.PENDING:
            set_outcome(session, cb_id, "ignored")
            return

        if claimed_ok:
            verified = q(checkout_request_id)
            vcode = verified.get("ResultCode") if isinstance(verified, dict) else None
            if vcode is None:
                # Not proof the payment failed: keep it pending; the reconciler re-queries.
                logger.warning("callback claimed SUCCESS but verification was inconclusive for %s "
                               "— left pending for reconciliation", checkout_request_id)
                set_outcome(session, cb_id, "left_pending")
                return
            if str(vcode) != "0":
                logger.warning("callback claimed SUCCESS but Safaricom disagreed for %s", checkout_request_id)
                mark_failed(eng, session, row.id, "verification_failed")
                set_outcome(session, cb_id, "applied")
                return

            # The amount MUST be present and MUST equal what this payment asked for.
            expected, reported = row.amount, data.get("Amount")
            verified_amount = verified.get("Amount") if isinstance(verified, dict) else None
            if reported is None:
                logger.error("callback for %s has no Amount — flagged for manual review", checkout_request_id)
                _flag_failed(eng, session, row, checkout_request_id, "amount_missing",
                             f"expected=KES {expected} reported=<none>")
                set_outcome(session, cb_id, "applied")
                return
            if expected is None or not amounts_match(expected, reported, verified_amount):
                logger.error("amount mismatch checkout_id=%s: expected %s got %s — marking FAILED "
                             "for manual review instead of crediting", checkout_request_id, expected, reported)
                _flag_failed(eng, session, row, checkout_request_id, "amount_mismatch",
                             f"expected=KES {expected} got=KES {reported}")
                set_outcome(session, cb_id, "applied")
                return

            finalize_successful_payment(eng, session, row.id, data.get("MpesaReceiptNumber"))
            set_outcome(session, cb_id, "applied")
            return

        # ── Failure callback: trusted only as far as Safaricom's own answer ──
        verified = q(checkout_request_id)
        vcode = verified.get("ResultCode") if isinstance(verified, dict) else None
        if vcode is not None and str(vcode) == "0":
            logger.error("FAILURE callback contradicted by Safaricom for %s — settling as SUCCESS",
                         checkout_request_id)
            if finalize_successful_payment(eng, session, row.id, None):
                eng.alert(f"Payment #{row.id} failure callback contradicted by Safaricom",
                          f"checkout_id={checkout_request_id} — settled as SUCCESS. Check for a forged "
                          "callback and consider rotating the callback secret.")
            set_outcome(session, cb_id, "recovered")
            return

        # Safaricom agrees (or could not answer right now). Record FAILED; if the answer was
        # inconclusive the reconciler re-checks it and recovers it if it was wrong.
        mark_failed(eng, session, row.id, result_desc or f"code {claimed_code}")
        set_outcome(session, cb_id, "applied")

    except Exception:
        session.rollback()
        logger.exception("callback processing failed for checkout_id=%s", checkout_request_id)


def process_b2b_callback(eng, session, body: dict, kind: str = "b2b") -> None:
    try:
        conversation_id, status, receipt, desc = parse_b2b_result(body)
        result = body.get("Result", {}) if isinstance(body, dict) else {}
        cb_id = record_callback(session, kind, conversation_id,
                                result.get("ResultCode") if isinstance(result, dict) else None, body)
        logger.info("%s callback received: conversation_id=%s status=%s", kind, conversation_id, status)
        if not conversation_id:
            logger.error("B2B callback has no ConversationID — cannot match to a disbursement, dropping")
            set_outcome(session, cb_id, "orphan")
            return
        if apply_b2b(session, kind, conversation_id, status, receipt, desc):
            set_outcome(session, cb_id, "applied")
        elif disbursement_exists(session, conversation_id):
            set_outcome(session, cb_id, "duplicate")        # already processed
        else:
            # Usually the result beat our own "store conversation_id" UPDATE. Keep it: disburse()
            # replays it a moment later, and so does the reconciler.
            logger.warning("%s callback conversation_id=%s matched no disbursement yet — kept for replay",
                           kind, conversation_id)
            set_outcome(session, cb_id, "unmatched", processed=False)
    except Exception:
        session.rollback()
        logger.exception("B2B callback processing failed")
