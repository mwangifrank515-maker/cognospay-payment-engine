import pytest

from cognospay_engine import (
    InvalidAmount,
    InvalidPhone,
    LinkExhausted,
    LinkExpired,
    LinkNotAutoChargeable,
    LinkNotFound,
    PaymentLink,
    PhoneThrottled,
    StkRejected,
    StkUnconfirmed,
)
from conftest import make_link, pay


def used(db, lid):
    return db.query(PaymentLink).filter_by(link_id=lid).one().used_count


def test_phone_normalised_and_single_use_enforced(eng, db, gw):
    lid = make_link(db, 100)
    r = eng.pay_link(db, lid, "0712345678")
    assert gw.stk == [("254712345678", 100, str(r.payment_id))] and used(db, lid) == 1
    with pytest.raises(LinkExhausted):
        eng.pay_link(db, lid, "0712345678")


def test_stale_link_row_cannot_double_claim(eng, db, gw):
    """Two requests that both passed the pre-check: the guarded UPDATE lets only one through."""
    from cognospay_engine import payments
    lid = make_link(db, 100)
    l1, l2 = eng.get_valid_link(db, link_id=lid), eng.get_valid_link(db, link_id=lid)
    payments._start_link_payment(eng, db, l1, "0712345678", 100)
    with pytest.raises(LinkExhausted):
        payments._start_link_payment(eng, db, l2, "0722000111", 100)
    assert len(gw.stk) == 1


def test_rejected_push_gives_the_use_back_and_fails_the_payment(eng, db, gw):
    lid = make_link(db, 100)
    gw.stk_result = None
    with pytest.raises(StkRejected):
        eng.pay_link(db, lid, "0712345678")
    assert used(db, lid) == 0
    gw.stk_result = {"CheckoutRequestID": "ws_CO_ok"}
    eng.pay_link(db, lid, "0712345678")


def test_timeout_is_unconfirmed_keeps_the_use_and_alerts(eng, db, gw, alerts):
    from cognospay_engine import Payment
    lid = make_link(db, 100)
    gw.stk_result = {"_unknown": True, "reason": "network"}
    with pytest.raises(StkUnconfirmed):
        eng.pay_link(db, lid, "0712345678")
    p = db.query(Payment).filter_by(link_id=lid).one()
    assert p.status == "unconfirmed" and p.result_desc.startswith("stk_unknown")
    assert used(db, lid) == 1                                    # NOT given back: customer may still pay
    assert any("outcome unknown" in s for s, _ in alerts)


def test_phone_throttle_treats_formats_as_one_number(eng, db, gw):
    limit = eng.settings.phone_throttle_limit
    variants = ["0712345678", "254712345678", "+254712345678", "712345678", "0712 345 678"]
    for v in variants[:limit]:
        eng.pay_link(db, make_link(db, 100), v)
    with pytest.raises(PhoneThrottled):
        eng.pay_link(db, make_link(db, 100), variants[limit])
    assert len(gw.stk) == limit


def test_fixed_amount_link_ignores_client_supplied_amount(eng, db, gw):
    eng.pay_link(db, make_link(db, 500), "0712345678", amount=1)
    assert gw.stk[0][1] == 500


def test_open_amount_link_requires_whole_shillings(eng, db, gw):
    lid = make_link(db, None)
    for bad in (99.5, 0, None):
        with pytest.raises(InvalidAmount):
            eng.pay_link(db, lid, "0712345678", amount=bad)
    assert not gw.stk
    eng.pay_link(db, lid, "0712345678", amount=250)
    assert gw.stk[0][1] == 250


def test_bad_phone_rejected_before_anything_is_written(eng, db, gw):
    from cognospay_engine import Payment
    lid = make_link(db, 100)
    for bad in ("12345", "", None):
        with pytest.raises(InvalidPhone):
            eng.pay_link(db, lid, bad)
    assert db.query(Payment).count() == 0 and used(db, lid) == 0 and not gw.stk


def test_link_lifecycle_errors(eng, db):
    from datetime import timedelta

    from cognospay_engine.clock import utcnow
    with pytest.raises(LinkNotFound):
        eng.get_valid_link(db, link_id="nope")
    with pytest.raises(LinkNotFound):
        eng.get_valid_link(db)
    lid = make_link(db, 100)
    db.query(PaymentLink).filter_by(link_id=lid).update({"expires_at": utcnow() - timedelta(minutes=1)})
    db.commit()
    with pytest.raises(LinkExpired):
        eng.get_valid_link(db, link_id=lid)


def test_create_link_validates_and_normalises(eng, db):
    out = eng.create_link(db, 7, amount=100, phone="0712345678", description="x", max_uses=0)
    row = db.query(PaymentLink).filter_by(link_id=out["link_id"]).one()
    assert row.phone == "254712345678" and row.max_uses == 1 and row.merchant_id == 7
    with pytest.raises(InvalidAmount):
        eng.create_link(db, 7, amount=99.5)
    with pytest.raises(InvalidPhone):
        eng.create_link(db, 7, phone="12")
    with pytest.raises(ValueError):
        eng.create_link(db, 7, channel="SMS")


def test_auto_charge_needs_phone_and_fixed_amount(eng, db, gw):
    open_amt = eng.create_link(db, 1, phone="0712345678")
    with pytest.raises(LinkNotAutoChargeable):
        eng.auto_charge_link(db, open_amt["token"])
    no_phone = eng.create_link(db, 1, amount=100)
    with pytest.raises(LinkNotAutoChargeable):
        eng.auto_charge_link(db, no_phone["token"])
    ok = eng.create_link(db, 1, amount=100, phone="0712345678")
    r = eng.auto_charge_link(db, ok["token"])
    assert gw.stk[0][0] == "254712345678" and pay(db, r.payment_id).status == "pending"


def test_direct_stk_records_reference_and_attribution(eng, db, gw):
    r = eng.initiate_stk(db, 9, "0712345678", 150, reference="INV-77", created_by_user_id=4)
    p = pay(db, r.payment_id)
    assert (p.merchant_id, p.reference, p.created_by_user_id, p.channel) == (9, "INV-77", 4, "STK")
    assert gw.stk[0][2] == "INV-77" and p.checkout_request_id == "ws_CO_mock"
    with pytest.raises(InvalidAmount):
        eng.initiate_stk(db, 9, "0722000111", 10.5)


def test_direct_stk_rejected_and_unconfirmed(eng, db, gw):
    gw.stk_result = None
    with pytest.raises(StkRejected):
        eng.initiate_stk(db, 9, "0712345678", 100)
    gw.stk_result = {"_unknown": True, "reason": "bad_json_http_502"}
    with pytest.raises(StkUnconfirmed):
        eng.initiate_stk(db, 9, "0722000111", 100)
