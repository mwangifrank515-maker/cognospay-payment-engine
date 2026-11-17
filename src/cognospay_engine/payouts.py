"""B2B payouts to merchants, with double-payout protection.

The disbursement ROW is inserted *first*, as ``initiating`` (``disbursements.payment_id`` is
UNIQUE), and only the caller that wins that insert is allowed to call B2B. Two overlapping
callers (callback + reconciliation job) can therefore never both pay out. A row stuck in
``initiating`` means "we may have sent it" and is surfaced for a human, never retried.
"""
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .clock import utcnow
from .fees import compute_fee_and_net
from .inbox import replay_b2b_inbox
from .models import Disbursement

logger = logging.getLogger("cognospay_engine.payouts")


def disburse(eng, session, payment_id: int, merchant_id: int, gross: float, *,
             existing_id: int | None = None, fixed_fee_net: tuple | None = None) -> None:
    """Deduct the fee and send the net amount to the merchant's Till/Paybill.

    Records the attempt in ``disbursements`` regardless of outcome. A no-op in collect-only mode
    (no ``payout_resolver``). ``existing_id`` retries an already-recorded FAILED row in place.
    """
    if eng.payout_resolver is None:
        return
    route = eng.payout_resolver(session, merchant_id)

    if fixed_fee_net is not None:
        fee, net = fixed_fee_net
    else:
        frac = route.fee_fraction if (route and route.fee_fraction is not None) \
            else eng.settings.default_fee_fraction
        fee, net = compute_fee_and_net(gross, frac)

    if route is None or not route.destination_number:
        logger.warning("merchant_id=%s has no payout destination configured — disbursement skipped",
                       merchant_id)
        if existing_id is not None:
            session.execute(update(Disbursement).where(Disbursement.id == existing_id).values(
                attempts=(Disbursement.attempts + 1), result_desc="no_destination_configured"))
        else:
            try:
                session.add(Disbursement(
                    payment_id=payment_id, merchant_id=merchant_id, gross_amount=gross,
                    fee_amount=fee, net_amount=net, destination_type="none", destination_number="",
                    status="failed", result_desc="no_destination_configured", attempts=1,
                    created_at=utcnow()))
                session.flush()
            except IntegrityError:
                session.rollback()
                return
        session.commit()
        return

    dest_type, dest_number = route.destination_type, route.destination_number

    # ── 1. CLAIM: only the winner of this write may send money ──────────────
    if existing_id is None:
        try:
            row = Disbursement(
                payment_id=payment_id, merchant_id=merchant_id, gross_amount=gross,
                fee_amount=fee, net_amount=net, destination_type=dest_type,
                destination_number=dest_number, status="initiating", result_desc="initiating",
                attempts=1, created_at=utcnow())
            session.add(row)
            session.commit()
            disb_id = row.id
        except IntegrityError:
            session.rollback()
            logger.info("disbursement for payment_id=%s already claimed — not sending again", payment_id)
            return
    else:
        won = session.execute(
            update(Disbursement).where(Disbursement.id == existing_id, Disbursement.status == "failed")
            .values(status="initiating", result_desc="initiating", destination_type=dest_type,
                    destination_number=dest_number, fee_amount=fee, net_amount=net,
                    conversation_id=None, attempts=Disbursement.attempts + 1))
        session.commit()
        if won.rowcount == 0:
            logger.info("disbursement id=%s is no longer 'failed' — retry skipped", existing_id)
            return
        disb_id = existing_id

    # ── 2. SEND (net is a whole number of shillings by construction) ────────
    try:
        send = eng.gateway.b2b_till if dest_type == "till" else eng.gateway.b2b_paybill
        result = send(net, dest_number, account_ref=f"PAY-{payment_id}")
    except Exception:
        logger.exception("B2B call raised for payment_id=%s", payment_id)
        result = None

    # This is the *accept* response. ResponseCode 0 only means Safaricom queued the request;
    # the real outcome arrives on the b2b result callback, matched via conversation_id.
    accepted = bool(result) and str(result.get("ResponseCode")) == "0"
    status = "pending" if accepted else "failed"
    if result:
        desc = result.get("ResponseDescription") or result.get("errorMessage") or "rejected"
    else:
        # No usable response (timeout / network): we cannot know whether Safaricom received it,
        # so this must NOT be auto-retried.
        desc = "api_error"
    conversation_id = (result or {}).get("ConversationID")

    logger.info("disbursement payment_id=%s merchant=%s dest=%s/%s net=%.2f accepted=%s conversation_id=%s",
                payment_id, merchant_id, dest_type, dest_number, net, accepted, conversation_id)

    # ── 3. RECORD the outcome on the claimed row ───────────────────────────
    session.execute(update(Disbursement).where(Disbursement.id == disb_id).values(
        status=status, result_desc=desc, conversation_id=conversation_id))
    session.commit()

    if accepted and conversation_id:
        try:    # a very fast result callback may have beaten this UPDATE
            replay_b2b_inbox(session, conversation_id)
        except Exception:
            session.rollback()
            logger.exception("b2b inbox replay failed for conversation_id=%s", conversation_id)

    if not accepted:
        logger.warning("B2B disbursement rejected for payment_id=%s — needs manual processing", payment_id)


def retry_disbursement(eng, session, disbursement_id: int) -> str:
    """Re-send a FAILED disbursement. Returns the resulting status.

    ONLY call this when the earlier failure is known to mean "money did not move" (no destination,
    explicit rejection, failed result callback). A row whose ``result_desc`` is in
    ``AMBIGUOUS_DESCS`` is ambiguous: check the Safaricom portal first, then resolve by hand."""
    d = session.execute(select(Disbursement).where(Disbursement.id == disbursement_id)
                        .execution_options(populate_existing=True)).scalar_one_or_none()
    if not d or d.status != "failed":
        return d.status if d else "not_found"
    fixed = None
    if d.net_amount is not None and float(d.net_amount) == int(d.net_amount):
        fixed = (float(d.fee_amount or 0), float(d.net_amount))   # re-send exactly what was recorded
    disburse(eng, session, d.payment_id, d.merchant_id, float(d.gross_amount),
             existing_id=d.id, fixed_fee_net=fixed)
    return session.execute(select(Disbursement.status).where(Disbursement.id == disbursement_id)
                           .execution_options(populate_existing=True)).scalar()
