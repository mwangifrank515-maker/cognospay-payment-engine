"""Settles payments/payouts that the happy path left in limbo.

The normal flow is: STK push -> Safaricom callback -> mark SUCCESS -> B2B payout -> B2B result
callback. Each arrow can silently fail:

  * the STK callback never arrives          -> payment stays 'pending' forever
  * the process dies after SUCCESS commits  -> paid, but no disbursement row
  * the B2B call is rejected                -> 'failed', nothing retried

Run :func:`run_all` every few minutes (see :func:`run_loop` / the CLI). It closes those gaps with
actions that cannot pay anyone twice: pending payments are settled from Safaricom's own answer,
the payout path claims its row first, and AMBIGUOUS rows ('initiating', 'api_error',
'queue_timeout') are reported to a human, never auto-retried, because money may have moved.
"""
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update

from .callbacks import finalize_successful_payment, mark_failed
from .clock import utcnow
from .constants import AMBIGUOUS_DESCS, DELIBERATE_FAIL_DESCS
from .inbox import replay_b2b_inbox
from .models import Disbursement, MpesaCallback, Payment, PaymentStatus
from .payouts import disburse, retry_disbursement

logger = logging.getLogger("cognospay_engine.reconciliation")


def reconcile_stale_payments(eng, session, now: datetime | None = None) -> dict:
    s, now = eng.settings, now or utcnow()
    rows = session.execute(select(Payment).where(
        Payment.status == PaymentStatus.PENDING, Payment.checkout_request_id.is_not(None),
        Payment.created_at < now - timedelta(minutes=s.stale_pending_minutes),
        Payment.created_at > now - timedelta(days=s.give_up_pending_days))
        .order_by(Payment.id).limit(s.batch)).scalars().all()
    out = {"checked": len(rows), "settled_success": 0, "settled_failed": 0, "still_unknown": 0}
    for r in [(p.id, p.checkout_request_id) for p in rows]:
        pid, cid = r
        try:
            answer = eng.gateway.query_transaction(cid)
            code = answer.get("ResultCode") if isinstance(answer, dict) else None
            if code is None:
                out["still_unknown"] += 1       # network error / "still processing"
                continue
            if str(code) == "0":
                # The STK query carries no receipt number.
                if finalize_successful_payment(eng, session, pid, None):
                    out["settled_success"] += 1
            else:
                if mark_failed(eng, session, pid, answer.get("ResultDesc") or f"code {code}"):
                    out["settled_failed"] += 1
        except Exception:
            session.rollback()
            logger.exception("reconciliation failed for payment_id=%s", pid)
    return out


def recheck_failed_payments(eng, session, now: datetime | None = None) -> dict:
    """A FAILED payment was usually set from a callback we could only partly verify. Re-ask
    Safaricom on a schedule; if it actually succeeded, recover it through the normal guarded path
    (pays out once) and alert. Payments flagged on purpose for a human are left alone."""
    s, now = eng.settings, now or utcnow()
    out = {"checked": 0, "recovered": 0}
    rows = session.execute(select(Payment).where(
        Payment.status == PaymentStatus.FAILED, Payment.checkout_request_id.is_not(None),
        func.coalesce(Payment.recheck_count, 0) < len(s.failed_recheck_schedule),
        Payment.created_at > now - timedelta(hours=s.failed_recheck_window_hours),
        func.coalesce(Payment.result_desc, "").not_in(list(DELIBERATE_FAIL_DESCS)))
        .order_by(Payment.id).limit(s.batch)).scalars().all()
    for r in [(p.id, p.checkout_request_id, int(p.recheck_count or 0), p.created_at) for p in rows]:
        pid, cid, n, created = r
        try:
            if now - created < s.failed_recheck_schedule[n]:
                continue
            out["checked"] += 1
            answer = eng.gateway.query_transaction(cid)
            code = answer.get("ResultCode") if isinstance(answer, dict) else None
            if code is None:
                continue                              # can't tell: try again next run
            if str(code) == "0":
                reopened = session.execute(update(Payment).where(
                    Payment.id == pid, Payment.status == PaymentStatus.FAILED)
                    .values(status=PaymentStatus.PENDING, result_desc="recovered_from_failed")
                    .execution_options(synchronize_session=False))
                session.commit()
                if reopened.rowcount and finalize_successful_payment(eng, session, pid, None):
                    out["recovered"] += 1
                    eng.alert(f"Payment #{pid} recovered from FAILED",
                              f"checkout_id={cid}: Safaricom reports success. Settled and paid out. "
                              "Look for a lost or forged callback.")
            else:
                session.execute(update(Payment).where(Payment.id == pid).values(
                    recheck_count=func.coalesce(Payment.recheck_count, 0) + 1)
                    .execution_options(synchronize_session=False))
                session.commit()
        except Exception:
            session.rollback()
            logger.exception("recheck of failed payment_id=%s failed", pid)
    return out


def purge_old_inbox(eng, session, now: datetime | None = None) -> int:
    """Callbacks hold phone numbers: keep resolved ones ``inbox_retention_days``, unresolved forever."""
    now = now or utcnow()
    res = session.execute(delete(MpesaCallback).where(
        MpesaCallback.created_at < now - timedelta(days=eng.settings.inbox_retention_days),
        MpesaCallback.outcome != "unmatched"))
    session.commit()
    return res.rowcount or 0


def ensure_disbursements(eng, session, now: datetime | None = None) -> int:
    """SUCCESS payments with no disbursement row at all (crash between the SUCCESS commit and the
    payout). Safe: disburse() claims the row first. No-op in collect-only mode."""
    if eng.payout_resolver is None:
        return 0
    s, now = eng.settings, now or utcnow()
    rows = session.execute(
        select(Payment.id, Payment.merchant_id, Payment.amount)
        .outerjoin(Disbursement, Disbursement.payment_id == Payment.id)
        .where(Payment.status == PaymentStatus.SUCCESS, Disbursement.id.is_(None),
               Payment.created_at < now - timedelta(minutes=s.missing_disbursement_minutes))
        .order_by(Payment.id).limit(s.batch)).all()
    for pid, mid, amount in rows:
        try:
            disburse(eng, session, pid, mid, float(amount))
        except Exception:
            session.rollback()
            logger.exception("could not create missing disbursement for payment_id=%s", pid)
    return len(rows)


def retry_failed_disbursements(eng, session, now: datetime | None = None) -> int:
    """Bounded automatic retry of payouts KNOWN not to have moved money. Ambiguous rows excluded."""
    if eng.payout_resolver is None:
        return 0
    s, now = eng.settings, now or utcnow()
    rows = session.execute(select(Disbursement.id, Disbursement.attempts, Disbursement.created_at).where(
        Disbursement.status == "failed", func.coalesce(Disbursement.attempts, 1) < s.max_auto_retries,
        func.coalesce(Disbursement.result_desc, "").not_in(list(AMBIGUOUS_DESCS)))
        .order_by(Disbursement.id).limit(s.batch)).all()
    retried = 0
    for did, attempts, created in rows:
        wait = timedelta(minutes=s.retry_spacing_minutes * max(1, int(attempts or 1)))
        if now - created < wait:
            continue
        try:
            retry_disbursement(eng, session, did)
            retried += 1
        except Exception:
            session.rollback()
            logger.exception("retry failed for disbursement id=%s", did)
    return retried


def find_needs_attention(eng, session, now: datetime | None = None) -> dict:
    """Things only a human can resolve; reported, never auto-actioned."""
    s, now = eng.settings, now or utcnow()

    def count(model, *conds):
        return session.execute(select(func.count()).select_from(model).where(*conds)).scalar() or 0

    D, P, C = Disbursement, Payment, MpesaCallback
    amb = list(AMBIGUOUS_DESCS)
    return {
        "payouts_ambiguous": count(D, or_(D.status == "initiating", (D.status == "failed")
                                          & D.result_desc.in_(["api_error", "queue_timeout"]))),
        "payouts_failed_exhausted": count(D, D.status == "failed",
                                          func.coalesce(D.attempts, 1) >= s.max_auto_retries,
                                          func.coalesce(D.result_desc, "").not_in(amb)),
        "payouts_pending_over_1h": count(D, D.status == "pending", D.created_at < now - timedelta(hours=1)),
        "payments_without_checkout_id": count(P, P.status == PaymentStatus.PENDING,
                                              P.checkout_request_id.is_(None),
                                              P.created_at < now - timedelta(minutes=10)),
        # STK request whose answer was lost: the customer may have paid.
        "payments_unconfirmed": count(P, P.status == PaymentStatus.UNCONFIRMED),
        # older than the reconciler's horizon: nothing will ever settle them on its own
        "payments_pending_stale": count(P, P.status == PaymentStatus.PENDING,
                                        P.created_at < now - timedelta(days=s.give_up_pending_days)),
        "success_callbacks_unmatched": count(C, C.kind == "stk", C.outcome == "unmatched",
                                             C.result_code == "0", C.created_at > now - timedelta(days=7)),
        "b2b_results_unmatched": count(C, C.kind.in_(("b2b", "b2b_timeout")), C.outcome == "unmatched",
                                       C.created_at < now - timedelta(hours=1)),
    }


def write_heartbeat(eng, now: datetime | None = None) -> None:
    """Proof of life: optional hook, optional marker file, optional external ping that alerts
    you when it STOPS arriving."""
    s, now = eng.settings, now or utcnow()
    if eng.hooks.heartbeat:
        try:
            eng.hooks.heartbeat(now)
        except Exception:
            logger.exception("heartbeat hook failed")
    if s.heartbeat_file:
        try:
            with open(s.heartbeat_file, "w") as f:
                f.write(now.isoformat())
        except OSError:
            pass
    if s.heartbeat_ping_url:
        try:
            import requests
            requests.get(s.heartbeat_ping_url, timeout=5)
        except Exception:
            logger.warning("heartbeat ping failed", exc_info=True)


def run_all(eng, session, now: datetime | None = None) -> dict:
    now = now or utcnow()
    summary = {
        "b2b_inbox_replayed": replay_b2b_inbox(session, older_than_seconds=60),
        "payments": reconcile_stale_payments(eng, session, now),
        "failed_rechecked": recheck_failed_payments(eng, session, now),
        "missing_disbursements_created": ensure_disbursements(eng, session, now),
        "disbursements_retried": retry_failed_disbursements(eng, session, now),
    }
    for i, job in enumerate(eng.hooks.maintenance):      # host-specific extras
        try:
            summary[f"maintenance_{getattr(job, '__name__', i)}"] = job(session, now)
        except Exception:
            session.rollback()
            logger.exception("maintenance job %r failed", job)
    attention = find_needs_attention(eng, session, now)
    summary["needs_attention"] = attention
    # Alert at most about once an hour (the job runs every few minutes).
    if any(attention.values()) and now.minute < eng.settings.attention_alert_minute_window:
        eng.alert("Payments need manual attention",
                  ", ".join(f"{k}={v}" for k, v in attention.items() if v))
    try:
        purge_old_inbox(eng, session, now)
    except Exception:
        session.rollback()
        logger.exception("inbox purge failed")
    write_heartbeat(eng, now)          # only reached when the run itself did not blow up
    logger.info("reconciliation: %s", summary)
    return summary


def run_loop(eng, session_factory, interval_seconds: int = 300) -> None:
    """Run forever. ``session_factory`` is e.g. ``sessionmaker(bind=engine)``."""
    while True:
        try:
            with session_factory() as session:
                run_all(eng, session)
        except Exception:
            logger.exception("reconciliation run failed; will retry")
        time.sleep(interval_seconds)
