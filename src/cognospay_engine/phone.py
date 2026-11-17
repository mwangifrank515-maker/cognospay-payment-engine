"""Kenyan MSISDN handling for M-Pesa.

Daraja only accepts the international form 2547XXXXXXXX / 2541XXXXXXXX. End users type
``07XXXXXXXX``, so every phone that reaches M-Pesa, and the per-phone throttle, goes through
:func:`normalize_msisdn` first. Without it ``0712...`` / ``254712...`` / ``+254712...`` would be
three different phones to the throttle.
"""
import re

from .errors import InvalidPhone

_KE_MSISDN = re.compile(r"254[17]\d{8}")


def normalize_msisdn(raw) -> str:
    """Return ``254XXXXXXXXX`` or raise :class:`InvalidPhone`."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00254"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if not _KE_MSISDN.fullmatch(digits):
        raise InvalidPhone()
    return digits


def mask_phone(phone: str | None) -> str:
    """``254712345678`` -> ``25471***78``. Use this in every log line and API response."""
    return phone[:5] + "***" + phone[-2:] if phone and len(phone) > 7 else "***"
