# Security

This library moves money. Please report vulnerabilities privately (GitHub "Report a vulnerability" on this
repository, or the security contact you publish) and do not open a public issue for them.

## Hardening checklist for integrators

- Callback URL secret: 24+ random characters, https only, rotate if a log ever leaks it. Call
  `install_callback_redaction()` so access logs never print it.
- Never take `merchant_id` from request input; derive it from your authentication.
- Never return a `CheckoutRequestID` or raw Safaricom payloads to an anonymous client.
- Treat `payments.phone` and `mpesa_callbacks.payload` as personal data (encryption at rest, retention).
- Optionally restrict the callback routes to Safaricom's published source IPs at your proxy; the engine does not.
- Don't commit `.env` files; `MPESA_SECURITY_CREDENTIAL` and the passkey are as sensitive as a password.
- Never retry an *ambiguous* payout (`initiating`, `api_error`, `queue_timeout`) without checking the Safaricom portal.
