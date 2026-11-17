"""Exceptions raised by the engine.

Every error carries an ``http_status`` so a web adapter can map it to a response without a
lookup table. Messages are safe to show to the end user (they never contain secrets).
"""


class EngineError(Exception):
    http_status = 500
    default_message = "Payment engine error"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class InvalidAmount(EngineError, ValueError):
    http_status = 400
    default_message = "Invalid amount"


class InvalidPhone(EngineError, ValueError):
    http_status = 400
    default_message = "Enter a valid Kenyan mobile number, e.g. 0712345678"


class LinkNotFound(EngineError):
    http_status = 404
    default_message = "Invalid link"


class LinkExpired(EngineError):
    http_status = 410
    default_message = "This payment link has expired"


class LinkExhausted(EngineError):
    http_status = 410
    default_message = "This payment link has already been used"


class LinkNotAutoChargeable(EngineError):
    http_status = 400
    default_message = "This link cannot be charged automatically"


class PhoneThrottled(EngineError):
    http_status = 429
    default_message = "Too many payment attempts to this phone number recently. Try again later."


class StkRejected(EngineError):
    http_status = 502
    default_message = "STK push was rejected by M-Pesa"


class StkUnconfirmed(EngineError):
    """The request MAY have reached Safaricom. The customer might still get a prompt and pay."""
    http_status = 503
    default_message = (
        "We couldn't confirm that the M-Pesa request was sent. If you receive a prompt and pay, "
        "the payment will still be recorded. Please don't pay twice; contact the merchant if you "
        "are unsure."
    )


class PaymentStartFailed(EngineError):
    http_status = 500
    default_message = "Could not start payment, please try again"
