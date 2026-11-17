"""Money rules for payouts, in one place.

M-Pesa moves WHOLE shillings (B2B ``Amount`` must be an integer). The rule is:
the fee is rounded half-up to a whole shilling, the net is floored to a whole shilling, and any
sub-shilling remainder goes into the fee so that

    gross == fee + net      (always)

and what we RECORD is exactly what we SEND.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .errors import InvalidAmount


def compute_fee_and_net(gross, fraction) -> tuple[float, float]:
    """``fraction`` is a fraction (0.02 == 2 %). Returns ``(fee, net)``; ``net`` is whole."""
    g = Decimal(str(gross))
    p = Decimal(str(fraction or 0))
    fee = (g * p).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    net = (g - fee).to_integral_value(rounding=ROUND_DOWN)
    if net < 0:
        net = Decimal(0)
    return float(g - net), float(net)


def require_whole_shillings(amount) -> int:
    """STK push amounts must be whole, positive shillings. Raises :class:`InvalidAmount`."""
    try:
        d = Decimal(str(amount))
    except Exception:
        raise InvalidAmount("Enter a whole-number amount in KES") from None
    if d != d.to_integral_value():
        raise InvalidAmount("Amount must be a whole number of shillings")
    if d <= 0:
        raise InvalidAmount("Amount must be positive")
    return int(d)


def amounts_match(expected, *reported) -> bool:
    """True when every non-None reported amount equals the expected one (+-0.005)."""
    e = Decimal(str(expected))
    for r in reported:
        if r is not None and abs(Decimal(str(r)) - e) > Decimal("0.005"):
            return False
    return True
