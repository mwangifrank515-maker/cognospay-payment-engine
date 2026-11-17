"""Configuration. Plain dataclasses: no global settings object, no framework dependency."""
import os
from dataclasses import dataclass, field
from datetime import timedelta

SANDBOX_URL = "https://sandbox.safaricom.co.ke"
PRODUCTION_URL = "https://api.safaricom.co.ke"
SANDBOX_SHORTCODE = "174379"


def _is_local(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))  # nosec B104 - detects localhost URLs


@dataclass
class DarajaConfig:
    consumer_key: str
    consumer_secret: str
    shortcode: str
    passkey: str
    callback_base_url: str          # PUBLIC https URL Safaricom can reach, e.g. https://pay.example.com
    callback_secret: str            # >= 24 chars; the only proof a callback came from Safaricom
    environment: str = "sandbox"    # "sandbox" | "production"
    base_url: str | None = None     # overrides ``environment`` when set
    initiator_name: str = ""        # B2B payouts only
    security_credential: str = ""   # B2B payouts only
    callback_path_prefix: str = "/callback"   # must match where you mount the callback router
    stk_transaction_type: str = "CustomerPayBillOnline"   # or CustomerBuyGoodsOnline (till)
    party_b: str | None = None      # STK PartyB; defaults to ``shortcode``
    b2b_remarks: str = "Disbursement"
    timeout: tuple = (5, 15)        # (connect, read) seconds

    @property
    def api_base(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return PRODUCTION_URL if self.environment == "production" else SANDBOX_URL

    @classmethod
    def from_env(cls, prefix: str = "MPESA_", environ=None) -> "DarajaConfig":
        """Reads MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET, MPESA_SHORTCODE, MPESA_PASSKEY,
        MPESA_CALLBACK_BASE_URL, MPESA_CALLBACK_SECRET and optionally MPESA_BASE_URL,
        MPESA_ENVIRONMENT, MPESA_INITIATOR_NAME, MPESA_SECURITY_CREDENTIAL."""
        e = environ if environ is not None else os.environ
        g = lambda k, d="": e.get(prefix + k, d)  # noqa: E731
        return cls(
            consumer_key=g("CONSUMER_KEY"), consumer_secret=g("CONSUMER_SECRET"),
            shortcode=g("SHORTCODE", SANDBOX_SHORTCODE), passkey=g("PASSKEY"),
            callback_base_url=g("CALLBACK_BASE_URL", "http://localhost:8000"),
            callback_secret=g("CALLBACK_SECRET"),
            environment=g("ENVIRONMENT", "sandbox"), base_url=g("BASE_URL") or None,
            initiator_name=g("INITIATOR_NAME"), security_credential=g("SECURITY_CREDENTIAL"),
        )


def production_problems(cfg: DarajaConfig, *, payouts: bool = True) -> list[str]:
    """Settings that are fine in development but silently wrong in production.

    Call at startup and refuse to boot (or log loudly) when the list is non-empty."""
    p: list[str] = []
    if "sandbox" in cfg.api_base.lower():
        p.append("Daraja base URL is the Safaricom SANDBOX: no real money moves")
    if cfg.shortcode == SANDBOX_SHORTCODE:
        p.append(f"shortcode is the sandbox test shortcode {SANDBOX_SHORTCODE}")
    required = ["consumer_key", "consumer_secret", "passkey"]
    if payouts:
        required += ["initiator_name", "security_credential"]
    for name in required:
        if not getattr(cfg, name):
            p.append(f"{name} is empty (STK push and/or B2B payouts cannot work)")
    if _is_local(cfg.callback_base_url) or not cfg.callback_base_url.lower().startswith("https://"):
        p.append("callback_base_url must be a public https:// URL Safaricom can reach "
                 "(callbacks to localhost never arrive; payments then settle only via the reconciler, late)")
    if len(cfg.callback_secret or "") < 24:
        p.append("callback_secret must be at least 24 characters (it is the only proof a callback "
                 "came from Safaricom)")
    return p


@dataclass
class EngineSettings:
    """Tunables. Defaults are the values CognosPay runs in production."""
    stk_description: str = "Payment"          # TransactionDesc sent with every STK push
    default_fee_fraction: float = 0.02        # used when a PayoutRoute does not set its own
    phone_throttle_limit: int = 3             # STK pushes per phone ...
    phone_throttle_window_minutes: int = 5    # ... in this window
    adopt_window_minutes: int = 90            # match a callback to an 'unconfirmed' payment
    # reconciliation
    stale_pending_minutes: int = 5            # wait this long for the real callback first
    give_up_pending_days: int = 7             # Daraja can't tell us about older ones
    missing_disbursement_minutes: int = 3
    max_auto_retries: int = 3
    retry_spacing_minutes: int = 15           # multiplied by attempts so far
    failed_recheck_schedule: tuple = field(
        default_factory=lambda: (timedelta(minutes=10), timedelta(hours=2), timedelta(hours=24)))
    failed_recheck_window_hours: int = 48
    inbox_retention_days: int = 90
    batch: int = 50
    attention_alert_minute_window: int = 10   # alert only in the first N minutes of the hour
    heartbeat_file: str | None = None         # e.g. a docker healthcheck marker
    heartbeat_ping_url: str | None = None     # external dead-man's-switch (healthchecks.io etc.)
