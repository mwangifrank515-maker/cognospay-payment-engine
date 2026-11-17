"""The :class:`PaymentEngine`: one object holding the gateway, hooks and policy.

Every public method takes a SQLAlchemy ``Session`` as its first argument and commits its own
unit of work, so it drops into a request-scoped session (FastAPI ``Depends``, Flask-SQLAlchemy,
a Celery task...) without the engine owning your connection handling.
"""
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from .config import EngineSettings
from .daraja import MpesaGateway
from .models import Payment

logger = logging.getLogger("cognospay_engine")


@dataclass(frozen=True)
class PayoutRoute:
    """Where a merchant's money goes after a successful payment, and what you keep.

    ``fee_fraction`` is a fraction (0.02 == 2 %). ``None`` means "use the engine default";
    ``0`` means "no per-transaction fee" (e.g. a flat monthly plan).
    """
    destination_type: str                 # "till" | "paybill"
    destination_number: str
    fee_fraction: Optional[float] = None


# (session, merchant_id) -> PayoutRoute, or None when the merchant has nowhere to be paid yet.
PayoutResolver = Callable[[Session, int], Optional[PayoutRoute]]


@dataclass
class Hooks:
    """Optional integration points. Every hook is wrapped: an exception is logged and rolled
    back, and never blocks the payment flow or the payout."""
    # Operational alerts for a human (unmatched money, amount mismatch...). Default: log only.
    alert: Optional[Callable[[str, str], None]] = None
    # Called once per payment, after SUCCESS is committed and BEFORE the payout. Mark your
    # invoice / order paid here. Runs exactly once (the status transition is a guarded UPDATE).
    on_payment_succeeded: Optional[Callable[[Session, Payment], None]] = None
    # Called once when a payment is settled as FAILED by a callback or the reconciler.
    on_payment_failed: Optional[Callable[[Session, Payment], None]] = None
    # Called at the end of every reconciliation run (proof of life).
    heartbeat: Optional[Callable[[datetime], None]] = None
    # Extra host jobs appended to each reconciliation run: fn(session, now) -> anything.
    maintenance: list = field(default_factory=list)


class PaymentEngine:
    def __init__(self, gateway: MpesaGateway, *, payout_resolver: Optional[PayoutResolver] = None,
                 hooks: Optional[Hooks] = None, settings: Optional[EngineSettings] = None,
                 callback_secret: Optional[str] = None):
        """``payout_resolver=None`` runs the engine in *collect-only* mode: payments are
        collected and verified, no B2B payout is ever attempted."""
        self.gateway = gateway
        self.payout_resolver = payout_resolver
        self.hooks = hooks or Hooks()
        self.settings = settings or EngineSettings()
        self.callback_secret = callback_secret or getattr(getattr(gateway, "config", None),
                                                          "callback_secret", None)

    # ── helpers used by the other modules ──────────────────────────────────
    def alert(self, subject: str, message: str) -> None:
        try:
            if self.hooks.alert:
                self.hooks.alert(subject, message)
            else:
                logger.warning("ALERT (no alert hook configured): %s — %s", subject, message)
        except Exception:
            logger.exception("alert hook failed: %s", subject)

    def fire(self, session: Session, hook_name: str, *args: Any) -> None:
        fn = getattr(self.hooks, hook_name, None)
        if not fn:
            return
        try:
            fn(session, *args)
        except Exception:
            session.rollback()
            logger.exception("hook %s failed", hook_name)

    def verify_callback_secret(self, secret: str) -> bool:
        if not self.callback_secret:
            return False
        return hmac.compare_digest(str(secret), str(self.callback_secret))

    # ── public API (thin delegates) ────────────────────────────────────────
    def create_link(self, session, merchant_id, **kw):
        from . import payments
        return payments.create_link(self, session, merchant_id, **kw)

    def get_valid_link(self, session, *, link_id=None, token=None):
        from . import payments
        return payments.get_valid_link(session, link_id=link_id, token=token)

    def pay_link(self, session, link_id, phone, amount=None):
        from . import payments
        return payments.pay_link(self, session, link_id, phone, amount)

    def auto_charge_link(self, session, token):
        from . import payments
        return payments.auto_charge_link(self, session, token)

    def initiate_stk(self, session, merchant_id, phone, amount, reference=None, created_by_user_id=None):
        from . import payments
        return payments.initiate_stk(self, session, merchant_id, phone, amount, reference, created_by_user_id)

    def handle_stk_callback(self, session, body):
        from . import callbacks
        return callbacks.process_stk_callback(self, session, body)

    def handle_b2b_callback(self, session, body, kind="b2b"):
        from . import callbacks
        return callbacks.process_b2b_callback(self, session, body, kind)

    def retry_disbursement(self, session, disbursement_id):
        from . import payouts
        return payouts.retry_disbursement(self, session, disbursement_id)

    def reconcile(self, session, now=None):
        from . import reconciliation
        return reconciliation.run_all(self, session, now)
