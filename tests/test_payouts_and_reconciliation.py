from datetime import timedelta

from sqlalchemy import select

from cognospay_engine import Disbursement, Hooks, PaymentEngine, PayoutRoute
from cognospay_engine.clock import utcnow
from cognospay_engine.payouts import disburse
from cognospay_engine.reconciliation import (
    ensure_disbursements,
    find_needs_attention,
    purge_old_inbox,
    recheck_failed_payments,
    reconcile_stale_payments,
    retry_failed_disbursements,
)
from conftest import SECRET, disb, make_payment, pay, stk_body


def test_only_one_caller_wins_the_claim(eng, db, gw, sf):
    pid, _ = make_payment(db, 100, status="SUCCESS")
    disburse(eng, db, pid, 1, 100.0)
    with sf() as other:                                           # second, independent session
        disburse(eng, other, pid, 1, 100.0)
    assert len(gw.b2b) == 1 and db.query(Disbursement).count() == 1


def test_paybill_destination_and_custom_fee(db, gw):
    e = PaymentEngine(gw, callback_secret=SECRET,
                      payout_resolver=lambda s, m: PayoutRoute("paybill", "600111", fee_fraction=0.05))
    pid, _ = make_payment(db, 1000, status="SUCCESS")
    disburse(e, db, pid, 1, 1000.0)
    d = disb(db, pid)
    assert gw.b2b == [("paybill", 950.0, "600111", f"PAY-{pid}")] and (d.fee_amount, d.net_amount) == (50, 950)


def test_zero_fee_route_pays_the_full_amount(db, gw):
    e = PaymentEngine(gw, callback_secret=SECRET, payout_resolver=lambda s, m: PayoutRoute("till", "1", fee_fraction=0))
    pid, _ = make_payment(db, 100, status="SUCCESS")
    disburse(e, db, pid, 1, 100.0)
    assert gw.b2b[0][1] == 100.0


def test_collect_only_mode_never_pays_out(db, gw):
    e = PaymentEngine(gw, callback_secret=SECRET)                 # no payout_resolver
    pid, cid = make_payment(db, 100)
    e.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).status == "SUCCESS" and not gw.b2b and disb(db, pid) is None
    assert ensure_disbursements(e, db, utcnow() + timedelta(hours=1)) == 0


def test_no_destination_is_recorded_then_retried_once_configured(db, gw):
    routes = {"r": None}
    e = PaymentEngine(gw, callback_secret=SECRET, payout_resolver=lambda s, m: routes["r"])
    pid, _ = make_payment(db, 100, status="SUCCESS")
    disburse(e, db, pid, 1, 100.0)
    d = disb(db, pid)
    assert (d.status, d.result_desc, d.destination_type) == ("failed", "no_destination_configured", "none") and not gw.b2b
    routes["r"] = PayoutRoute("till", "555111")                   # merchant finally adds a till
    assert retry_failed_disbursements(e, db, utcnow() + timedelta(hours=1)) == 1
    d = disb(db, pid)
    assert d.status == "pending" and d.attempts == 2 and len(gw.b2b) == 1


def test_explicit_rejection_is_retried_boundedly_but_timeout_is_not(eng, db, gw):
    pid, _ = make_payment(db, 100, status="SUCCESS")
    gw.b2b_result = {"ResponseCode": "2001", "ResponseDescription": "bad initiator"}
    disburse(eng, db, pid, 1, 100.0)
    assert (disb(db, pid).status, disb(db, pid).result_desc) == ("failed", "bad initiator")
    later = utcnow() + timedelta(days=1)
    for _ in range(5):
        retry_failed_disbursements(eng, db, later)
    assert disb(db, pid).attempts == eng.settings.max_auto_retries and len(gw.b2b) == eng.settings.max_auto_retries

    pid2, _ = make_payment(db, 100, status="SUCCESS")
    gw.b2b_result = None                                          # network error: ambiguous
    before = len(gw.b2b)
    disburse(eng, db, pid2, 1, 100.0)
    assert disb(db, pid2).result_desc == "api_error"
    retry_failed_disbursements(eng, db, later)
    assert len(gw.b2b) == before + 1                              # only the original send; never retried


def test_oauth_unavailable_payout_is_a_definite_failure_that_may_be_retried(eng, db, gw):
    pid, _ = make_payment(db, 100, status="SUCCESS")
    gw.b2b_result = {"ResponseCode": "-1", "ResponseDescription": "oauth_unavailable", "_not_sent": True}
    disburse(eng, db, pid, 1, 100.0)
    gw.b2b_result = {"ResponseCode": "0", "ConversationID": "AG_9"}
    retry_failed_disbursements(eng, db, utcnow() + timedelta(hours=1))
    assert disb(db, pid).status == "pending"


def test_raising_gateway_is_treated_as_ambiguous(eng, db, gw):
    pid, _ = make_payment(db, 100, status="SUCCESS")
    def boom(*a, **k): raise RuntimeError("socket")
    gw.b2b_till = boom
    disburse(eng, db, pid, 1, 100.0)
    assert disb(db, pid).result_desc == "api_error"


# ── reconciliation ───────────────────────────────────────────────────────────
def test_stale_pending_is_settled_from_safaricoms_answer(eng, db, gw):
    ok, _ = make_payment(db, 100, age_min=10)
    bad, _ = make_payment(db, 100, age_min=10)
    fresh, _ = make_payment(db, 100, age_min=1)
    answers = iter([{"ResultCode": "0"}, {"ResultCode": "1032", "ResultDesc": "cancelled"}])
    gw.query_result = None
    gw.query_transaction = lambda cid: next(answers)
    out = reconcile_stale_payments(eng, db)
    assert out == {"checked": 2, "settled_success": 1, "settled_failed": 1, "still_unknown": 0}
    assert pay(db, ok).status == "SUCCESS" and pay(db, bad).status == "FAILED" and pay(db, fresh).status == "pending"
    assert len(gw.b2b) == 1


def test_still_processing_is_left_alone(eng, db, gw):
    pid, _ = make_payment(db, 100, age_min=10)
    gw.query_result = {"errorCode": "500.001.1001"}               # no ResultCode: still processing
    assert reconcile_stale_payments(eng, db)["still_unknown"] == 1 and pay(db, pid).status == "pending"


def test_failed_payment_recovered_by_recheck_schedule(eng, db, gw, alerts):
    pid, _ = make_payment(db, 100, status="FAILED", age_min=15)
    assert recheck_failed_payments(eng, db) == {"checked": 1, "recovered": 1}
    assert pay(db, pid).status == "SUCCESS" and len(gw.b2b) == 1 and any("recovered" in s for s, _ in alerts)


def test_recheck_respects_schedule_and_deliberate_failures(eng, db, gw):
    young, _ = make_payment(db, 100, status="FAILED", age_min=3)        # before the 10 min mark
    flagged, _ = make_payment(db, 100, status="FAILED", age_min=60)
    from cognospay_engine import Payment
    db.query(Payment).filter_by(id=flagged).update({"result_desc": "amount_mismatch"}); db.commit()
    gw.query_result = {"ResultCode": "1032"}
    assert recheck_failed_payments(eng, db)["checked"] == 0
    old, _ = make_payment(db, 100, status="FAILED", age_min=15)
    assert recheck_failed_payments(eng, db) == {"checked": 1, "recovered": 0}
    assert pay(db, old).recheck_count == 1 and pay(db, flagged).status == "FAILED" and not gw.b2b


def test_missing_disbursement_is_created_without_double_paying(eng, db, gw):
    lost, _ = make_payment(db, 100, status="SUCCESS", age_min=10)       # crashed before payout
    done, _ = make_payment(db, 100, status="SUCCESS", age_min=10)
    disburse(eng, db, done, 1, 100.0)
    n = len(gw.b2b)
    assert ensure_disbursements(eng, db) == 1
    assert disb(db, lost) is not None and len(gw.b2b) == n + 1
    assert ensure_disbursements(eng, db) == 0


def test_needs_attention_reports_what_only_a_human_can_fix(eng, db, gw):
    make_payment(db, 100, status="unconfirmed", age_min=1)
    pid, _ = make_payment(db, 100, status="SUCCESS")
    db.add(Disbursement(payment_id=pid, merchant_id=1, status="initiating", attempts=1)); db.commit()
    a = find_needs_attention(eng, db)
    assert a["payments_unconfirmed"] == 1 and a["payouts_ambiguous"] == 1 and a["payments_pending_stale"] == 0


def test_run_all_alerts_in_the_alert_window_and_writes_heartbeat(db, gw, alerts, tmp_path):
    hb = []
    e = PaymentEngine(gw, callback_secret=SECRET, hooks=Hooks(alert=lambda s, m: alerts.append((s, m)),
                                                              heartbeat=hb.append, maintenance=[lambda s, n: "ran"]))
    e.settings.heartbeat_file = str(tmp_path / "ok")
    make_payment(db, 100, status="unconfirmed")
    out = e.reconcile(db, utcnow().replace(minute=3))
    assert out["maintenance_<lambda>"] == "ran" and hb and (tmp_path / "ok").exists()
    assert any(s == "Payments need manual attention" for s, _ in alerts)


def test_purge_keeps_unmatched_and_recent_callbacks(eng, db):
    from cognospay_engine import MpesaCallback
    old = utcnow() - timedelta(days=100)
    for outcome in ("applied", "unmatched"):
        db.add(MpesaCallback(kind="stk", ref=outcome, outcome=outcome, created_at=old))
    db.add(MpesaCallback(kind="stk", ref="new", outcome="applied")); db.commit()
    assert purge_old_inbox(eng, db) == 1
    assert {r.ref for r in db.execute(select(MpesaCallback)).scalars()} == {"unmatched", "new"}
