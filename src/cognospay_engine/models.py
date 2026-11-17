"""Database models.

Table and column names match the ones CognosPay already runs in production, so an existing
database can adopt the engine without a data migration. ``merchant_id`` is a plain integer
tenant key (no foreign key: the engine does not own your merchants table).

All four tables hang off :data:`Base`; pass ``Base.metadata`` to Alembic, or call
``Base.metadata.create_all(engine)`` in development.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .clock import utcnow
from .sqltypes import Money


class Base(DeclarativeBase):
    pass


class PaymentStatus:
    PENDING = "pending"          # STK request sent, waiting for Safaricom
    UNCONFIRMED = "unconfirmed"  # STK request MAY have been sent (timeout); never treat as failed
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class DisbursementStatus:
    INITIATING = "initiating"    # row claimed, B2B call in flight: money MAY have moved
    PENDING = "pending"          # Safaricom accepted, waiting for the result callback
    SENT = "sent"
    FAILED = "failed"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    merchant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    amount: Mapped[float] = mapped_column(Money, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str | None] = mapped_column(String, default=PaymentStatus.PENDING)
    mpesa_receipt: Mapped[str | None] = mapped_column(String)
    checkout_request_id: Mapped[str | None] = mapped_column(String, index=True)
    result_desc: Mapped[str | None] = mapped_column(String)
    reference: Mapped[str | None] = mapped_column(String)
    link_id: Mapped[str | None] = mapped_column(String, index=True)
    channel: Mapped[str | None] = mapped_column(String, default="STK")  # STK | Link | QR
    # Optional attribution for your own staff accounts; the engine never interprets it.
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Times the reconciler re-checked this payment with Safaricom after it was marked FAILED.
    recheck_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)


class Disbursement(Base):
    """One row per payout sent to a merchant after a successful payment.

    ``payment_id`` is UNIQUE: that constraint is what makes the claim-first payout safe.
    """
    __tablename__ = "disbursements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), unique=True)
    merchant_id: Mapped[int | None] = mapped_column(Integer)
    gross_amount: Mapped[float | None] = mapped_column(Money)
    fee_amount: Mapped[float | None] = mapped_column(Money)
    net_amount: Mapped[float | None] = mapped_column(Money)
    destination_type: Mapped[str | None] = mapped_column(String)    # till | paybill | none
    destination_number: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, default=DisbursementStatus.PENDING)
    mpesa_receipt: Mapped[str | None] = mapped_column(String)
    result_desc: Mapped[str | None] = mapped_column(String)
    # ConversationID from the B2B *accept* response; the later result callback carries it back.
    conversation_id: Mapped[str | None] = mapped_column(String, index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PaymentLink(Base):
    __tablename__ = "payment_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    link_id: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    merchant_id: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[float | None] = mapped_column(Money, nullable=True)   # None = payer enters it
    description: Mapped[str | None] = mapped_column(String)
    token: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String)
    channel: Mapped[str | None] = mapped_column(String, default="Link")  # Link | QR
    max_uses: Mapped[int | None] = mapped_column(Integer, default=1)
    used_count: Mapped[int | None] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)


class MpesaCallback(Base):
    """Inbox of every raw Safaricom callback (STK result, B2B result, B2B queue timeout).

    A callback that matches nothing is a row, not a log line: an unmatched SUCCESS is a visible,
    alertable fact, and a B2B result that beats its disbursement's ``conversation_id`` can be
    replayed instead of dropped. ``payload`` holds phone numbers: see ``inbox_retention_days``.
    """
    __tablename__ = "mpesa_callbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)     # stk | b2b | b2b_timeout
    ref: Mapped[str | None] = mapped_column(String)               # CheckoutRequestID / ConversationID
    result_code: Mapped[str | None] = mapped_column(String)
    payload: Mapped[str | None] = mapped_column(Text)
    # received | applied | duplicate | unmatched | adopted | left_pending | recovered | ignored | orphan
    outcome: Mapped[str] = mapped_column(String, nullable=False, default="received")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_mpesa_callbacks_kind_ref", "kind", "ref"),
        Index("ix_mpesa_callbacks_outcome", "outcome", "created_at"),
    )
