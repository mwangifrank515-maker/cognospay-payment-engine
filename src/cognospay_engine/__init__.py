"""cognospay-engine: a safety-first M-Pesa (Safaricom Daraja) payment engine.

STK push collection, payment links, verified callbacks, claim-first B2B payouts and a
reconciliation job. Framework-agnostic; SQLAlchemy for storage.
"""
from .callbacks import finalize_successful_payment
from .config import DarajaConfig, EngineSettings, production_problems
from .daraja import DarajaClient, MpesaGateway
from .engine import Hooks, PaymentEngine, PayoutRoute
from .errors import (
                     EngineError,
                     InvalidAmount,
                     InvalidPhone,
                     LinkExhausted,
                     LinkExpired,
                     LinkNotAutoChargeable,
                     LinkNotFound,
                     PaymentStartFailed,
                     PhoneThrottled,
                     StkRejected,
                     StkUnconfirmed,
)
from .fees import compute_fee_and_net, require_whole_shillings
from .models import Base, Disbursement, DisbursementStatus, MpesaCallback, Payment, PaymentLink, PaymentStatus
from .payments import PaymentStarted
from .phone import mask_phone, normalize_msisdn
from .redaction import install_callback_redaction

__version__ = "0.1.0"

__all__ = [
    "PaymentEngine", "Hooks", "PayoutRoute", "DarajaClient", "MpesaGateway", "DarajaConfig",
    "EngineSettings", "production_problems", "Base", "Payment", "PaymentLink", "Disbursement",
    "MpesaCallback", "PaymentStatus", "DisbursementStatus", "PaymentStarted",
    "finalize_successful_payment", "compute_fee_and_net", "require_whole_shillings",
    "normalize_msisdn", "mask_phone", "install_callback_redaction", "EngineError", "InvalidAmount",
    "InvalidPhone", "LinkNotFound", "LinkExpired", "LinkExhausted", "LinkNotAutoChargeable",
    "PhoneThrottled", "StkRejected", "StkUnconfirmed", "PaymentStartFailed", "__version__",
]
