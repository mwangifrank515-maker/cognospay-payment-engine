from cognospay_engine import Disbursement
from conftest import disb, inbox, make_payment, pay, stk_body


def test_success_callback_verifies_credits_pays_out_once_and_fires_hook(eng, db, gw, events):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 100, receipt="RCPX"))
    p = pay(db, pid)
    assert p.status == "SUCCESS" and p.mpesa_receipt == "RCPX"
    assert gw.queries == [cid]                                           # cross-verified with Safaricom
    d = disb(db, pid)
    assert (d.gross_amount, d.fee_amount, d.net_amount, d.status) == (100, 2, 98, "pending")
    assert gw.b2b == [("till", 98.0, "555111", f"PAY-{pid}")]
    assert events["succeeded"] == [pid] and inbox(db)[-1].outcome == "applied"


def test_duplicate_callback_never_pays_twice(eng, db, gw, events):
    pid, cid = make_payment(db, 100)
    for _ in range(3):
        eng.handle_stk_callback(db, stk_body(cid, 100))
    assert len(gw.b2b) == 1 and events["succeeded"] == [pid]
    assert [r.outcome for r in inbox(db)] == ["applied", "duplicate", "duplicate"]


def test_amount_mismatch_is_flagged_not_credited(eng, db, gw, alerts, events):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 1))
    p = pay(db, pid)
    assert p.status == "FAILED" and p.result_desc == "amount_mismatch" and not gw.b2b
    assert any(f"Payment #{pid} amount_mismatch" in s for s, _ in alerts) and events["failed"] == [pid]


def test_missing_amount_is_flagged_not_credited(eng, db, gw, alerts):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, None))
    assert pay(db, pid).result_desc == "amount_missing" and not gw.b2b
    assert any("amount_missing" in s for s, _ in alerts)


def test_verified_amount_disagreeing_with_callback_is_flagged(eng, db, gw):
    pid, cid = make_payment(db, 100)
    gw.query_result = {"ResultCode": "0", "Amount": 5}
    eng.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).result_desc == "amount_mismatch" and not gw.b2b


def test_forged_success_that_safaricom_denies_is_not_credited(eng, db, gw):
    pid, cid = make_payment(db, 100)
    gw.query_result = {"ResultCode": "1032"}
    eng.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).result_desc == "verification_failed" and not gw.b2b


def test_unverifiable_success_stays_pending_for_the_reconciler(eng, db, gw):
    pid, cid = make_payment(db, 100)
    gw.query_result = None
    eng.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).status == "pending" and not gw.b2b and inbox(db)[-1].outcome == "left_pending"


def test_genuine_failure_callback_fails_the_payment(eng, db, gw, events):
    pid, cid = make_payment(db, 100)
    gw.query_result = {"ResultCode": "1032"}
    eng.handle_stk_callback(db, stk_body(cid, code=1032))
    assert pay(db, pid).status == "FAILED" and events["failed"] == [pid] and not gw.b2b


def test_forged_failure_callback_cannot_fail_a_payment_safaricom_says_succeeded(eng, db, gw, alerts, events):
    pid, cid = make_payment(db, 100)
    gw.query_result = {"ResultCode": "0"}
    eng.handle_stk_callback(db, stk_body(cid, code=1032))
    assert pay(db, pid).status == "SUCCESS" and len(gw.b2b) == 1 and events["succeeded"] == [pid]
    assert any("contradicted" in s for s, _ in alerts) and inbox(db)[-1].outcome == "recovered"


def test_failure_recorded_while_safaricom_unreachable_is_failed_then_rechecked(eng, db, gw):
    pid, cid = make_payment(db, 100)
    gw.query_result = None
    eng.handle_stk_callback(db, stk_body(cid, code=1))
    assert pay(db, pid).status == "FAILED"


def test_late_success_reopens_a_failed_payment_after_verification(eng, db, gw):
    pid, cid = make_payment(db, 100, status="FAILED")
    eng.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).status == "SUCCESS" and len(gw.b2b) == 1


def test_a_payment_flagged_for_a_human_is_never_reopened(eng, db, gw):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 1))                    # -> amount_mismatch
    eng.handle_stk_callback(db, stk_body(cid, 100))                  # a later "correct" callback
    assert pay(db, pid).status == "FAILED" and not gw.b2b and inbox(db)[-1].outcome == "ignored"


def test_unmatched_success_is_stored_alerted_and_masks_the_phone(eng, db, gw, alerts, caplog):
    eng.handle_stk_callback(db, stk_body("ws_CO_nobody", 100, phone="254712345678"))
    assert inbox(db)[-1].outcome == "unmatched"
    assert any("Unmatched M-Pesa SUCCESS" in s for s, _ in alerts)
    assert all("254712345678" not in m for _, m in alerts)
    assert "254712345678" not in caplog.text                         # PII stays out of logs


def test_lost_stk_answer_is_adopted_by_the_real_callback(eng, db, gw):
    pid, _ = make_payment(db, 100, status="unconfirmed", cid="x")
    from cognospay_engine import Payment
    db.query(Payment).filter_by(id=pid).update({"checkout_request_id": None}); db.commit()
    eng.handle_stk_callback(db, stk_body("ws_CO_late", 100, receipt="RCPL", phone="254712345678"))
    p = pay(db, pid)
    assert p.status == "SUCCESS" and p.checkout_request_id == "ws_CO_late" and p.mpesa_receipt == "RCPL"
    assert len(gw.b2b) == 1


def test_adoption_needs_exactly_one_matching_candidate(eng, db, gw):
    from cognospay_engine import Payment
    ids = [make_payment(db, 100, status="unconfirmed", cid="x")[0] for _ in range(2)]
    db.query(Payment).update({"checkout_request_id": None}); db.commit()
    eng.handle_stk_callback(db, stk_body("ws_CO_amb", 100, phone="254712345678"))
    assert {pay(db, i).status for i in ids} == {"unconfirmed"} and inbox(db)[-1].outcome == "unmatched"


def test_reconciler_settled_payment_gets_its_receipt_when_the_callback_arrives(eng, db, gw):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, {"Body": {"stkCallback": {"CheckoutRequestID": cid, "ResultCode": 0}}})  # no Amount
    assert pay(db, pid).result_desc == "amount_missing"
    pid2, cid2 = make_payment(db, 100, age_min=10)
    eng.reconcile(db)
    assert pay(db, pid2).status == "SUCCESS" and pay(db, pid2).mpesa_receipt is None
    eng.handle_stk_callback(db, stk_body(cid2, 100, receipt="RCPLATE"))
    assert pay(db, pid2).mpesa_receipt == "RCPLATE" and len(gw.b2b) == 1


def test_malformed_bodies_are_recorded_not_raised(eng, db):
    for body in ({}, {"Body": {}}, None, [], {"Body": {"stkCallback": "x"}}):
        eng.handle_stk_callback(db, body)
    eng.handle_b2b_callback(db, {"nonsense": 1}); eng.handle_b2b_callback(db, None)
    assert len(inbox(db)) >= 5


def test_a_failing_success_hook_never_blocks_the_payout(eng, db, gw):
    def boom(sess, p): raise RuntimeError("invoice system down")
    eng.hooks.on_payment_succeeded = boom
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 100))
    assert pay(db, pid).status == "SUCCESS" and len(gw.b2b) == 1


# ── B2B result handling ──────────────────────────────────────────────────────
def b2b_body(conv, code=0, receipt="B2BRCP"):
    return {"Result": {"ConversationID": conv, "ResultCode": code, "ResultDesc": "done",
                       "ResultParameters": {"ResultParameter": [{"Key": "TransactionReceipt", "Value": receipt}]}}}


def test_b2b_success_and_failure_results_update_the_disbursement(eng, db, gw):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 100))
    eng.handle_b2b_callback(db, b2b_body("AG_1", 0))
    d = disb(db, pid)
    assert d.status == "sent" and d.mpesa_receipt == "B2BRCP"
    pid2, cid2 = make_payment(db, 100); gw.b2b_result = {"ResponseCode": "0", "ConversationID": "AG_2"}
    eng.handle_stk_callback(db, stk_body(cid2, 100))
    eng.handle_b2b_callback(db, b2b_body("AG_2", 2001))
    assert disb(db, pid2).status == "failed"


def test_b2b_result_that_beats_our_bookkeeping_is_replayed_not_lost(eng, db, gw):
    eng.handle_b2b_callback(db, b2b_body("AG_FAST", 0))          # arrives before any row has this id
    assert inbox(db, "b2b")[-1].outcome == "unmatched"
    gw.b2b_result = {"ResponseCode": "0", "ConversationID": "AG_FAST"}
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 100))              # disburse() replays the inbox itself
    assert disb(db, pid).status == "sent" and inbox(db, "b2b")[-1].outcome == "applied"


def test_reconciler_replays_an_unmatched_b2b_result(eng, db, gw):
    from datetime import timedelta

    from cognospay_engine import MpesaCallback
    from cognospay_engine.clock import utcnow
    pid, cid = make_payment(db, 100)
    gw.b2b_result = {"ResponseCode": "0", "ConversationID": "AG_LATE"}
    eng.handle_stk_callback(db, stk_body(cid, 100))
    db.query(Disbursement).update({"conversation_id": None, "status": "pending"}); db.commit()
    eng.handle_b2b_callback(db, b2b_body("AG_LATE", 0))
    db.query(Disbursement).update({"conversation_id": "AG_LATE"})
    db.query(MpesaCallback).update({"created_at": utcnow() - timedelta(minutes=5)}); db.commit()
    eng.reconcile(db)
    assert disb(db, pid).status == "sent"


def test_queue_timeout_is_ambiguous_and_never_auto_retried(eng, db, gw):
    pid, cid = make_payment(db, 100)
    eng.handle_stk_callback(db, stk_body(cid, 100))
    eng.handle_b2b_callback(db, {"Result": {"ConversationID": "AG_1", "ResultCode": 1, "ResultDesc": "t"}}, "b2b_timeout")
    d = disb(db, pid)
    assert (d.status, d.result_desc) == ("failed", "queue_timeout")
    from datetime import timedelta

    from cognospay_engine.clock import utcnow
    from cognospay_engine.reconciliation import retry_failed_disbursements
    assert retry_failed_disbursements(eng, db, utcnow() + timedelta(days=2)) == 0 and len(gw.b2b) == 1


# ── races: the guarded transition is what makes these safe ───────────────────
def test_finalize_is_exactly_once_even_when_called_twice(eng, db, gw, events):
    """A callback and the reconciler can both decide 'SUCCESS' for the same payment."""
    from cognospay_engine import finalize_successful_payment
    pid, _ = make_payment(db, 100)
    assert finalize_successful_payment(eng, db, pid, "R1") is True
    assert finalize_successful_payment(eng, db, pid, "R2") is False
    assert events["succeeded"] == [pid] and len(gw.b2b) == 1 and pay(db, pid).mpesa_receipt == "R1"


def test_callback_racing_the_reconciler_with_stale_state_does_not_double_credit(eng, db, sf, gw, events):
    """Session A loaded the payment while it was still 'pending'; session B (reconciler) then settles
    it. A's late finalize must be a no-op."""
    from cognospay_engine import finalize_successful_payment
    pid, cid = make_payment(db, 100)
    with sf() as other:
        stale = pay(other, pid)
        assert stale.status == "pending"
        assert finalize_successful_payment(eng, db, pid, None) is True            # reconciler wins
        assert finalize_successful_payment(eng, other, pid, "RLATE") is False     # stale caller loses
    assert events["succeeded"] == [pid] and len(gw.b2b) == 1


def test_failed_transition_does_not_clobber_a_payment_that_succeeded_meanwhile(eng, db, gw, events):
    from cognospay_engine.callbacks import mark_failed
    pid, _ = make_payment(db, 100, status="SUCCESS")
    assert mark_failed(eng, db, pid, "late failure") is False
    assert pay(db, pid).status == "SUCCESS" and events["failed"] == []
