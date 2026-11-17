"""The callback inbox and B2B result handling (shared by callbacks, payouts, reconciliation)."""
import json
import logging
from datetime import timedelta

from sqlalchemy import select, update

from .clock import utcnow
from .models import Disbursement, MpesaCallback

logger = logging.getLogger("cognospay_engine.inbox")


def record_callback(session, kind: str, ref, result_code, body) -> int | None:
    """Persist the raw callback BEFORE acting on it. Never raises."""
    try:
        row = MpesaCallback(
            kind=kind, ref=str(ref) if ref is not None else None,
            result_code=str(result_code) if result_code is not None else None,
            payload=json.dumps(body, default=str)[:20000], outcome="received")
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        logger.exception("could not record %s callback in the inbox", kind)
        return None


def set_outcome(session, cb_id, outcome: str, processed: bool = True) -> None:
    if cb_id is None:
        return
    try:
        session.execute(update(MpesaCallback).where(MpesaCallback.id == cb_id)
                        .values(outcome=outcome, processed_at=utcnow() if processed else None))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("could not update inbox row %s", cb_id)


def parse_b2b_result(body):
    """-> (conversation_id, status, receipt, desc)"""
    result = body.get("Result", {}) if isinstance(body, dict) else {}
    result = result if isinstance(result, dict) else {}
    conversation_id = result.get("ConversationID") or result.get("OriginatorConversationID")
    receipt = None
    params = (result.get("ResultParameters") or {}).get("ResultParameter", [])
    if isinstance(params, dict):
        params = [params]
    for prm in params or []:
        if isinstance(prm, dict) and prm.get("Key") == "TransactionReceipt":
            receipt = prm.get("Value")
    # Daraja sends the code as int OR string; compare as text.
    status = "sent" if str(result.get("ResultCode")) == "0" else "failed"
    return conversation_id, status, receipt, result.get("ResultDesc", "")


def apply_b2b(session, kind: str, conversation_id: str, status: str, receipt, desc) -> int:
    base = update(Disbursement).where(Disbursement.conversation_id == conversation_id,
                                      Disbursement.status == "pending")
    if kind == "b2b_timeout":
        res = session.execute(base.values(status="failed", result_desc="queue_timeout"))
    else:
        res = session.execute(base.values(status=status, mpesa_receipt=receipt, result_desc=desc))
    session.commit()
    return res.rowcount or 0


def disbursement_exists(session, conversation_id: str) -> bool:
    return session.execute(select(Disbursement.id).where(
        Disbursement.conversation_id == conversation_id).limit(1)).first() is not None


def replay_b2b_inbox(session, conversation_id: str | None = None, *,
                     older_than_seconds: int = 0, max_age_days: int = 7) -> dict:
    """Apply B2B results/timeouts that arrived before their disbursement row had a
    conversation_id. Safe to call repeatedly."""
    out = {"applied": 0, "duplicate": 0, "still_unmatched": 0}
    q = select(MpesaCallback).where(
        MpesaCallback.kind.in_(("b2b", "b2b_timeout")), MpesaCallback.outcome == "unmatched",
        MpesaCallback.created_at < utcnow() - timedelta(seconds=older_than_seconds),
        MpesaCallback.created_at > utcnow() - timedelta(days=max_age_days))
    if conversation_id:
        q = q.where(MpesaCallback.ref == conversation_id)
    for r in session.execute(q.order_by(MpesaCallback.id).limit(200)).scalars().all():
        try:
            conv, status, receipt, desc = parse_b2b_result(json.loads(r.payload or "{}"))
            if not conv:
                continue
            if apply_b2b(session, r.kind, conv, status, receipt, desc):
                set_outcome(session, r.id, "applied")
                out["applied"] += 1
            elif disbursement_exists(session, conv):
                set_outcome(session, r.id, "duplicate")
                out["duplicate"] += 1
            else:
                out["still_unmatched"] += 1
        except Exception:
            session.rollback()
            logger.exception("replay of inbox row %s failed", r.id)
    return out
