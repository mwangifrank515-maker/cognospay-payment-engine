import pytest

from cognospay_engine import (
    DarajaConfig,
    InvalidAmount,
    InvalidPhone,
    compute_fee_and_net,
    mask_phone,
    normalize_msisdn,
    production_problems,
    require_whole_shillings,
)
from cognospay_engine.redaction import redact_callback_paths


@pytest.mark.parametrize("raw", ["0712345678", "254712345678", "+254712345678", "712345678",
                                 "0712 345 678", "00254712345678", "0112345678"])
def test_phone_variants_collapse_to_one_form(raw):
    assert normalize_msisdn(raw) in ("254712345678", "254112345678")


@pytest.mark.parametrize("raw", ["12345", "", None, "0612345678", "25471234567", "abc"])
def test_bad_phones_rejected(raw):
    with pytest.raises(InvalidPhone):
        normalize_msisdn(raw)


def test_mask_phone():
    assert mask_phone("254712345678") == "25471***78" and mask_phone("123") == "***" and mask_phone(None) == "***"


@pytest.mark.parametrize("gross", [1, 7, 50, 99, 100, 101, 999, 12345, 1_000_000])
@pytest.mark.parametrize("pct", [0, 0.01, 0.02, 0.025, 0.1])
def test_fee_plus_net_always_equals_gross_and_net_is_whole(gross, pct):
    fee, net = compute_fee_and_net(gross, pct)
    assert fee + net == gross and net == int(net) and net >= 0


def test_fee_examples():
    assert compute_fee_and_net(100, 0.02) == (2.0, 98.0)
    assert compute_fee_and_net(99, 0.02) == (2.0, 97.0)      # fee rounds half-up to whole KES
    assert compute_fee_and_net(100, 0) == (0.0, 100.0)


@pytest.mark.parametrize("bad", [99.5, 0, -5, "abc", None])
def test_whole_shillings_rejected(bad):
    with pytest.raises(InvalidAmount):
        require_whole_shillings(bad)


def test_whole_shillings_accepts_float_integers():
    assert require_whole_shillings(100.0) == 100 and require_whole_shillings("250") == 250


def test_callback_secret_redacted_from_paths():
    assert redact_callback_paths("POST /callback/stk/supersecret HTTP/1.1") == "POST /callback/stk/[redacted] HTTP/1.1"
    assert "supersecret" not in redact_callback_paths("/callback/b2b-timeout/supersecret?x=1")


def test_production_problems_flags_sandbox_defaults_and_passes_a_good_config():
    bad = DarajaConfig("k", "s", "174379", "pk", "http://localhost:8000", "short")
    probs = " | ".join(production_problems(bad))
    for needle in ("SANDBOX", "174379", "callback_base_url", "callback_secret", "initiator_name"):
        assert needle in probs
    good = DarajaConfig("k", "s", "600123", "pk", "https://pay.example.com", "x" * 30,
                        environment="production", initiator_name="api", security_credential="cred")
    assert production_problems(good) == []
    assert production_problems(DarajaConfig("k", "s", "600123", "pk", "https://pay.example.com", "x" * 30,
                                            environment="production"), payouts=False) == []


def test_from_env_reads_the_same_names_cognospay_uses():
    c = DarajaConfig.from_env(environ={"MPESA_CONSUMER_KEY": "a", "MPESA_CONSUMER_SECRET": "b",
                                       "MPESA_SHORTCODE": "600123", "MPESA_PASSKEY": "p",
                                       "MPESA_CALLBACK_BASE_URL": "https://x.io", "MPESA_CALLBACK_SECRET": "z" * 24,
                                       "MPESA_ENVIRONMENT": "production"})
    assert c.api_base == "https://api.safaricom.co.ke" and c.shortcode == "600123"
