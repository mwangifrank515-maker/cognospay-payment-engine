"""Walk the whole money flow offline, with a simulated Safaricom. Run: python examples/simulate.py"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cognospay_engine import Base, Disbursement, Hooks, Payment, PaymentEngine, PayoutRoute
from cognospay_engine.testing import FakeGateway, b2b_result_body, stk_callback_body

db_engine = create_engine("sqlite://")
Base.metadata.create_all(db_engine)
session = sessionmaker(bind=db_engine)()

gateway = FakeGateway()                      # swap for DarajaClient(DarajaConfig.from_env()) in real life
engine = PaymentEngine(
    gateway,
    callback_secret="demo-secret-demo-secret-1234",
    payout_resolver=lambda s, merchant_id: PayoutRoute("till", "555111", fee_fraction=0.02),
    hooks=Hooks(on_payment_succeeded=lambda s, p: print(f"  hook: mark order paid (payment #{p.id}, receipt {p.mpesa_receipt})"),
                alert=lambda subject, msg: print(f"  ALERT: {subject}")),
)

link = engine.create_link(session, merchant_id=1, amount=1000, description="Order #42")
print("1. payment link:", link["link_id"])

started = engine.pay_link(session, link["link_id"], phone="0712345678")
print("2. STK push sent to", gateway.stk[0][0], "-> payment", started.payment_id)

checkout_id = session.get(Payment, started.payment_id).checkout_request_id
print("3. Safaricom calls back with SUCCESS")
engine.handle_stk_callback(session, stk_callback_body(checkout_id, amount=1000, receipt="SIM123ABC"))

d = session.query(Disbursement).filter_by(payment_id=started.payment_id).one()
print(f"4. payout: gross {d.gross_amount:.0f}  fee {d.fee_amount:.0f}  net {d.net_amount:.0f} -> till {d.destination_number} [{d.status}]")

print("5. Safaricom reports the payout result")
engine.handle_b2b_callback(session, b2b_result_body("AG_1", receipt="B2B999"))
session.refresh(d)
print("   payout status:", d.status, d.mpesa_receipt)

print("6. a replayed callback changes nothing (no second payout):")
engine.handle_stk_callback(session, stk_callback_body(checkout_id, amount=1000))
print("   B2B calls made:", len(gateway.b2b))
