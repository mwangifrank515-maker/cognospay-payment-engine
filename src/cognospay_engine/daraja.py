"""Thin Safaricom Daraja client.

Design rules (each learned the hard way):

* The OAuth token is cached (it lives ~1 h) instead of fetched before every call. A 401 /
  invalid-token answer drops the cache and retries once (safe: Daraja rejected it before doing
  anything).
* Timeouts are (connect 5 s, read 15 s).
* :meth:`stk_push` tells the caller apart:

      None                    -> nothing was sent (no OAuth token / clear 4xx). Safe to fail.
      {"_unknown": True, ...} -> the request MAY have reached Safaricom (timeout, 5xx without an
                                 error body, unreadable answer). The customer could still get a
                                 prompt and pay, so the caller must NOT treat it as failed.
      dict                    -> Safaricom's answer (CheckoutRequestID present = accepted,
                                 otherwise an explicit rejection).

* ``b2b_*`` return ``{"_not_sent": True, ...}`` when no token could be obtained (money did not
  move, safe to retry) and ``None`` only when the request may have been sent (ambiguous).
"""
import base64
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Protocol

import requests

from .config import DarajaConfig

logger = logging.getLogger("cognospay_engine.daraja")

# Safaricom's edge rejects some default client User-Agents; send a browser-like one.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}
_TOKEN_SAFETY_MARGIN = 120   # refresh this many seconds before Daraja says it expires
_DEFAULT_TOKEN_TTL = 3000
# Documented Daraja limits (longer values are truncated rather than risk a rejection).
_MAX_ACCOUNT_REF = 12
_MAX_TX_DESC = 13


class MpesaGateway(Protocol):
    """What the engine needs from a Daraja client. Implement this to fake or swap the network."""

    def stk_push(self, phone: str, amount, account_ref: str, transaction_desc: str) -> Optional[dict]: ...
    def query_transaction(self, checkout_request_id: str) -> Optional[dict]: ...
    def b2b_paybill(self, amount, destination_shortcode: str, account_ref: str) -> Optional[dict]: ...
    def b2b_till(self, amount, till_number: str, account_ref: str) -> Optional[dict]: ...


class DarajaClient:
    def __init__(self, config: DarajaConfig, http=None):
        self.config = config
        self._http = http or requests
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0

    # ── callback URLs ──────────────────────────────────────────────────────
    def _cb(self, name: str) -> str:
        c = self.config
        return f"{c.callback_base_url.rstrip('/')}{c.callback_path_prefix}/{name}/{c.callback_secret}"

    @property
    def stk_callback_url(self) -> str:
        return self._cb("stk")

    @property
    def b2b_result_url(self) -> str:
        return self._cb("b2b")

    @property
    def b2b_timeout_url(self) -> str:
        # Separate path: a queue-timeout notification must not be mistaken for a result.
        return self._cb("b2b-timeout")

    # ── auth ───────────────────────────────────────────────────────────────
    def _invalidate_token(self) -> None:
        with self._lock:
            self._token, self._expires_at = None, 0.0

    def _fetch_token(self) -> Optional[tuple]:
        c = self.config
        url = f"{c.api_base}/oauth/v1/generate?grant_type=client_credentials"
        for attempt in range(2):
            try:
                r = self._http.get(url, auth=(c.consumer_key, c.consumer_secret),
                                   headers=_HEADERS, timeout=c.timeout)
            except requests.RequestException:
                logger.warning("M-Pesa OAuth request failed (attempt %d/2)", attempt + 1, exc_info=True)
                continue
            if r.status_code != 200:
                logger.warning("M-Pesa OAuth returned %s: %s", r.status_code, (r.text or "")[:200])
                continue
            try:
                body = r.json()
            except ValueError:
                logger.warning("M-Pesa OAuth response wasn't valid JSON (attempt %d/2)", attempt + 1)
                continue
            token = body.get("access_token")
            if not token:
                continue
            try:
                ttl = int(body.get("expires_in") or _DEFAULT_TOKEN_TTL)
            except (TypeError, ValueError):
                ttl = _DEFAULT_TOKEN_TTL
            return token, ttl
        return None

    def get_access_token(self, force: bool = False) -> Optional[str]:
        """Cached bearer token. Only one thread refreshes at a time."""
        with self._lock:
            if not force and self._token and time.time() < self._expires_at:
                return self._token
            fetched = self._fetch_token()
            if not fetched:
                logger.error("M-Pesa OAuth failed after retries")
                self._token, self._expires_at = None, 0.0
                return None
            token, ttl = fetched
            self._token = token
            self._expires_at = time.time() + max(30, ttl - _TOKEN_SAFETY_MARGIN)
            return token

    @staticmethod
    def _is_invalid_token(res) -> bool:
        return res.status_code == 401 or "404.001.03" in (res.text or "")[:400]

    def _authed_post(self, url: str, payload: dict):
        """Returns (response, outcome); outcome is 'ok' | 'no_token' | 'network'."""
        res = None
        for attempt in range(2):
            token = self.get_access_token(force=(attempt == 1))
            if not token:
                return None, "no_token"
            headers = {"Authorization": f"Bearer {token}", **_HEADERS}
            try:
                res = self._http.post(url, json=payload, headers=headers, timeout=self.config.timeout)
            except requests.RequestException:
                logger.exception("M-Pesa request to %s failed (network/timeout)", url.rsplit("/", 1)[-1])
                return None, "network"
            if attempt == 0 and self._is_invalid_token(res):
                self._invalidate_token()
                continue
            break
        return res, "ok"

    def _password_and_timestamp(self):
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        c = self.config
        pw = base64.b64encode((c.shortcode + c.passkey + ts).encode()).decode()
        return pw, ts

    # ── STK push ───────────────────────────────────────────────────────────
    def stk_push(self, phone: str, amount, account_ref: str, transaction_desc: str) -> Optional[dict]:
        c = self.config
        password, timestamp = self._password_and_timestamp()
        url = f"{c.api_base}/mpesa/stkpush/v1/processrequest"
        payload = {
            "BusinessShortCode": c.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": c.stk_transaction_type,
            "Amount": int(amount),          # Daraja STK takes whole shillings only
            "PartyA": phone,
            "PartyB": c.party_b or c.shortcode,
            "PhoneNumber": phone,
            "CallBackURL": self.stk_callback_url,
            "AccountReference": str(account_ref)[:_MAX_ACCOUNT_REF],
            "TransactionDesc": str(transaction_desc)[:_MAX_TX_DESC],
        }
        res, outcome = self._authed_post(url, payload)
        if outcome == "no_token":
            return None
        if outcome == "network":
            return {"_unknown": True, "reason": "network"}
        try:
            body = res.json()
        except ValueError:
            logger.error("M-Pesa STK push response wasn't valid JSON (status=%s): %s",
                         res.status_code, (res.text or "")[:500])
            if 400 <= res.status_code < 500:
                return None                       # clearly rejected
            return {"_unknown": True, "reason": f"bad_json_http_{res.status_code}"}
        if not isinstance(body, dict):
            return {"_unknown": True, "reason": "unexpected_body"}
        if body.get("CheckoutRequestID"):
            return body
        logger.error("M-Pesa STK push rejected (status=%s): %s", res.status_code, body)
        if res.status_code >= 500 and "errorCode" not in body and "ResponseCode" not in body:
            return {"_unknown": True, "reason": f"http_{res.status_code}"}
        return body

    def query_transaction(self, checkout_request_id: str) -> Optional[dict]:
        """STK push status query. None when Safaricom could not be asked / answered."""
        c = self.config
        password, timestamp = self._password_and_timestamp()
        url = f"{c.api_base}/mpesa/stkpushquery/v1/query"
        payload = {"BusinessShortCode": c.shortcode, "Password": password,
                   "Timestamp": timestamp, "CheckoutRequestID": checkout_request_id}
        res, outcome = self._authed_post(url, payload)
        if outcome != "ok":
            return None
        try:
            return res.json()
        except ValueError:
            logger.exception("M-Pesa transaction query response wasn't valid JSON")
            return None

    # ── B2B payouts ────────────────────────────────────────────────────────
    def _b2b(self, command_id: str, receiver_type: str, amount, destination: str, account_ref: str):
        c = self.config
        url = f"{c.api_base}/mpesa/b2b/v1/paymentrequest"
        payload = {
            "Initiator": c.initiator_name,
            "SecurityCredential": c.security_credential,
            "CommandID": command_id,
            "SenderIdentifierType": "4",
            "RecieverIdentifierType": receiver_type,   # sic: Daraja's own spelling
            "Amount": int(amount),                      # B2B requires an integer
            "PartyA": c.shortcode,
            "PartyB": destination,
            "AccountReference": str(account_ref)[:_MAX_ACCOUNT_REF],
            "Remarks": c.b2b_remarks,
            "QueueTimeOutURL": self.b2b_timeout_url,
            "ResultURL": self.b2b_result_url,
        }
        res, outcome = self._authed_post(url, payload)
        if outcome == "no_token":
            # Nothing was sent, so money did not move: a definite failure, safe to retry.
            return {"ResponseCode": "-1", "ResponseDescription": "oauth_unavailable", "_not_sent": True}
        if outcome == "network":
            return None                      # may have been sent: ambiguous
        try:
            return res.json()
        except ValueError:
            logger.exception("M-Pesa B2B %s response wasn't valid JSON", command_id)
            return None

    def b2b_paybill(self, amount, destination_shortcode: str, account_ref: str) -> Optional[dict]:
        """Send funds from your shortcode to a merchant's Paybill (BusinessPayBill)."""
        return self._b2b("BusinessPayBill", "4", amount, destination_shortcode, account_ref)

    def b2b_till(self, amount, till_number: str, account_ref: str) -> Optional[dict]:
        """Send funds from your shortcode to a merchant's Till (BusinessBuyGoods)."""
        return self._b2b("BusinessBuyGoods", "2", amount, till_number, account_ref)
