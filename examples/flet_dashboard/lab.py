"""Engine Lab: a headless controller that drives the REAL engine against a simulated Safaricom.

No Flet import here, so everything the dashboard shows is testable without a screen. Every step a
scenario takes is recorded together with a snapshot of the database, so the UI can replay the
story and watch the tables change step by step. Narration is derived from the engine's actual
state after each call, never scripted: if the engine behaved differently, the story would say so.
"""
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import create_engine, desc, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cognospay_engine import (
    Base,
    Disbursement,
    EngineError,
    Hooks,
    MpesaCallback,
    Payment,
    PaymentEngine,
    PaymentLink,
    PayoutRoute,
    StkUnconfirmed,
    mask_phone,
)
from cognospay_engine.clock import utcnow
from cognospay_engine.reconciliation import find_needs_attention
from cognospay_engine.testing import FakeGateway, b2b_result_body, stk_callback_body

# (key, label, material-icon name) in the order money flows
NODES = [
    ("link", "Payment link", "LINK"),
    ("stk", "STK push", "PHONELINK_RING"),
    ("safaricom", "Safaricom", "CLOUD"),
    ("callback", "Callback", "WEBHOOK"),
    ("verify", "Verify", "FACT_CHECK"),
    ("success", "SUCCESS", "CHECK_CIRCLE"),
    ("hook", "Your hook", "EXTENSION"),
    ("payout", "B2B payout", "ACCOUNT_BALANCE"),
    ("result", "Payout result", "RECEIPT_LONG"),
    ("reconciler", "Reconciler", "AUTORENEW"),
]

PAYMENT_STATUSES = ("pending", "unconfirmed", "SUCCESS", "FAILED")
PAYOUT_STATUSES = ("initiating", "pending", "sent", "failed")

ATTENTION_HELP = {
    "payouts_ambiguous": "A payout may already have moved money (timeout/unknown). Check the Safaricom portal; it is never auto-retried.",
    "payouts_failed_exhausted": "A payout failed and automatic retries are used up.",
    "payouts_pending_over_1h": "Safaricom accepted a payout but no result arrived for over an hour.",
    "payments_without_checkout_id": "A payment was created but its STK push was never recorded.",
    "payments_unconfirmed": "An STK request's answer was lost. The customer may have paid.",
    "payments_pending_stale": "A payment is pending beyond the reconciler's horizon.",
    "success_callbacks_unmatched": "A customer paid but no payment row matches. Match it by hand.",
    "b2b_results_unmatched": "A payout result matched no payout.",
}


@dataclass
class Step:
    actor: str            # Your app | Customer | Safaricom | Engine | Reconciler | Operator | title
    text: str
    level: str = "info"   # info | ok | warn | bad | title
    node: str | None = None
    frame: dict | None = None


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    blurb: str
    tag: str              # Happy path | Attack | Failure | Recovery


SCENARIOS = [
    Scenario("happy", "Happy path", "Link → STK push → callback → verified → payout → payout result.", "Happy path"),
    Scenario("duplicate", "Safaricom repeats a callback", "The same SUCCESS arrives three times. One credit, one payout.", "Attack"),
    Scenario("wrong_amount", "Wrong amount", "Callback says KES 1 for a KES 100 payment. Flagged, not credited.", "Attack"),
    Scenario("forged_success", "Forged SUCCESS", "Someone posts a fake success. Safaricom says it never happened.", "Attack"),
    Scenario("forged_failure", "Forged FAILURE", "A fake 'cancelled' for a payment that really succeeded.", "Attack"),
    Scenario("single_use", "Single-use link", "Double-spend blocked; a rejected push gives the use back.", "Attack"),
    Scenario("throttle", "STK spam to one phone", "0712…, 254712…, +254712… are one phone to the throttle.", "Attack"),
    Scenario("stk_timeout", "STK request times out", "Marked unconfirmed (not failed). The late callback is adopted.", "Failure"),
    Scenario("lost_callback", "Callback never arrives", "The reconciler asks Safaricom and settles the payment.", "Recovery"),
    Scenario("crash", "Crash before payout", "Customer paid, payout never created. The reconciler creates it once.", "Recovery"),
    Scenario("b2b_timeout", "Payout times out", "Ambiguous: money may have moved. Never auto-retried.", "Failure"),
    Scenario("b2b_reject", "Payout rejected", "Bounded automatic retries, then a human, then a manual retry.", "Recovery"),
]


class SimGateway(FakeGateway):
    """Simulated Safaricom with three switches. Every call is logged on the lab's wire."""

    def __init__(self, lab):
        super().__init__()
        self.lab = lab
        self.stk_mode = "accept"       # accept | timeout | reject
        self.query_mode = "success"    # success | failed | unknown
        self.b2b_mode = "accept"       # accept | reject | timeout
        self.last_query = ""
        self._n = 0

    def stk_push(self, phone, amount, account_ref, transaction_desc):
        self.stk.append((phone, amount, account_ref))
        self._n += 1
        if self.stk_mode == "timeout":
            self.lab._wire("→", "STK push", f"{mask_phone(phone)} KES {amount:,}: no answer (timeout)", "warn")
            return {"_unknown": True, "reason": "network"}
        if self.stk_mode == "reject":
            self.lab._wire("→", "STK push", f"{mask_phone(phone)} KES {amount:,}: rejected", "bad")
            return None
        cid = f"ws_CO_{self._n:04d}"
        self.lab._wire("→", "STK push", f"{mask_phone(phone)} KES {amount:,} → {cid}", "info")
        return {"CheckoutRequestID": cid}

    def query_transaction(self, checkout_request_id):
        self.queries.append(checkout_request_id)
        if self.query_mode == "unknown":
            self.last_query = "no answer"
            self.lab._wire("→", "STK query", f"{checkout_request_id}: no answer", "warn")
            return None
        if self.query_mode == "failed":
            self.last_query = "FAILED (cancelled by user)"
            self.lab._wire("→", "STK query", f"{checkout_request_id}: not paid (1032)", "bad")
            return {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}
        self.last_query = "SUCCESS"
        self.lab._wire("→", "STK query", f"{checkout_request_id}: paid (0)", "ok")
        return {"ResultCode": "0"}

    def _b2b(self, kind, amount, dest, account_ref):
        self.b2b.append((kind, amount, dest, account_ref))
        self._n += 1
        label = f"KES {amount:,.0f} → {kind} {dest}"
        if self.b2b_mode == "timeout":
            self.lab._wire("→", "B2B payout", f"{label}: no answer (timeout)", "warn")
            return None
        if self.b2b_mode == "reject":
            self.lab._wire("→", "B2B payout", f"{label}: rejected", "bad")
            return {"ResponseCode": "2001", "ResponseDescription": "The initiator information is invalid"}
        conv = f"AG_{self._n:04d}"
        self.lab._wire("→", "B2B payout", f"{label} accepted → {conv}", "ok")
        return {"ResponseCode": "0", "ConversationID": conv}

    def b2b_till(self, amount, till_number, account_ref):
        return self._b2b("till", amount, till_number, account_ref)

    def b2b_paybill(self, amount, destination_shortcode, account_ref):
        return self._b2b("paybill", amount, destination_shortcode, account_ref)


def kes(x) -> str:
    return f"KES {float(x):,.0f}"


class Lab:
    MERCHANT_ID = 1
    FEE = 0.02

    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def reset(self):
        with self.lock:
            db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            Base.metadata.create_all(db)
            self.s = sessionmaker(bind=db)()
            self.wire, self.events, self.steps, self.lit = [], [], [], []
            self.payout_down = False
            self._phone_n = 0
            self._order = 1000
            self.gateway = SimGateway(self)
            self.engine = PaymentEngine(
                self.gateway, callback_secret="lab-secret-lab-secret-123456",
                payout_resolver=self._resolver,
                hooks=Hooks(alert=self._on_alert, on_payment_succeeded=self._on_succeeded,
                            on_payment_failed=self._on_failed))

    def _resolver(self, session, merchant_id):
        if self.payout_down:
            raise RuntimeError("process crashed before the payout")
        return PayoutRoute("till", "555111", self.FEE)

    # ── logs ───────────────────────────────────────────────────────────────
    @staticmethod
    def _t():
        return utcnow().strftime("%H:%M:%S")

    def _wire(self, direction, kind, text, level="info"):
        self.wire.append({"t": self._t(), "dir": direction, "kind": kind, "text": text, "level": level})

    def _on_alert(self, subject, message):
        self.events.append({"t": self._t(), "kind": "alert", "text": subject, "pid": None})

    def _on_succeeded(self, session, p):
        self.events.append({"t": self._t(), "kind": "hook", "pid": p.id,
                            "text": f"on_payment_succeeded(payment #{p.id}, {kes(p.amount)})"})

    def _on_failed(self, session, p):
        self.events.append({"t": self._t(), "kind": "hook", "pid": p.id,
                            "text": f"on_payment_failed(payment #{p.id}: {p.result_desc})"})

    def say(self, actor, text, level="info", node=None):
        if node and node not in self.lit:
            self.lit.append(node)
        self.steps.append(Step(actor, text, level, node, self.snapshot()))

    # ── read side ──────────────────────────────────────────────────────────
    @staticmethod
    def _age(dt):
        if dt is None:
            return ""
        m = int((utcnow() - dt).total_seconds() // 60)
        return "now" if m < 1 else f"{m}m" if m < 60 else f"{m // 60}h" if m < 1440 else f"{m // 1440}d"

    def snapshot(self) -> dict:
        s = self.s
        payments = s.execute(select(Payment).order_by(desc(Payment.id)).limit(30)
                             .execution_options(populate_existing=True)).scalars().all()
        payouts = s.execute(select(Disbursement).order_by(desc(Disbursement.id)).limit(30)
                            .execution_options(populate_existing=True)).scalars().all()
        inbox = s.execute(select(MpesaCallback).order_by(desc(MpesaCallback.id)).limit(30)
                          .execution_options(populate_existing=True)).scalars().all()
        pc = {k: 0 for k in PAYMENT_STATUSES}
        for st in s.execute(select(Payment.status)).scalars():
            pc[st] = pc.get(st, 0) + 1
        dc = {k: 0 for k in PAYOUT_STATUSES}
        for st in s.execute(select(Disbursement.status)).scalars():
            dc[st] = dc.get(st, 0) + 1
        return {
            "payments": [{"id": p.id, "amount": p.amount, "phone": mask_phone(p.phone), "status": p.status,
                          "receipt": p.mpesa_receipt or "", "note": p.result_desc or "",
                          "cid": p.checkout_request_id or "", "age": self._age(p.created_at)} for p in payments],
            "payouts": [{"id": d.id, "payment_id": d.payment_id, "gross": d.gross_amount, "fee": d.fee_amount,
                         "net": d.net_amount, "status": d.status, "note": d.result_desc or "",
                         "dest": f"{d.destination_type}:{d.destination_number}" if d.destination_type != "none" else "no destination",
                         "attempts": d.attempts, "conv": d.conversation_id or ""} for d in payouts],
            "inbox": [{"id": c.id, "kind": c.kind, "ref": c.ref or "", "code": c.result_code or "",
                       "outcome": c.outcome} for c in inbox],
            "wire": list(self.wire[-60:]),
            "events": list(self.events[-60:]),
            "attention": find_needs_attention(self.engine, s),
            "counts": {"payments": pc, "payouts": dc},
            "stk_calls": len(self.gateway.stk), "b2b_calls": len(self.gateway.b2b),
            "alerts": sum(1 for e in self.events if e["kind"] == "alert"),
        }

    # ── helpers ────────────────────────────────────────────────────────────
    def _p(self, pid):
        return self.s.execute(select(Payment).where(Payment.id == pid)
                              .execution_options(populate_existing=True)).scalar_one()

    def _d(self, pid):
        return self.s.execute(select(Disbursement).where(Disbursement.payment_id == pid)
                              .execution_options(populate_existing=True)).scalar_one_or_none()

    def _phone(self):
        self._phone_n += 1
        return f"0710{self._phone_n:06d}"

    def _receipt(self, pid):
        return f"SIM{pid:04d}K9"

    def _link(self, amount, **kw):
        self._order += 1
        link = self.engine.create_link(self.s, self.MERCHANT_ID, amount=amount,
                                       description=f"Order #{self._order}", **kw)
        self.say("Your app", f"Creates a single-use payment link for {kes(amount)} (Order #{self._order}).", node="link")
        return link

    def _latest_payment_id(self):
        return self.s.execute(select(Payment.id).order_by(desc(Payment.id)).limit(1)).scalar()

    def _start(self, amount=1000, phone=None, link=None):
        """Create a link and push. Returns the payment id, or None if the engine refused."""
        phone = phone or self._phone()
        link = link or self._link(amount)
        before = self._latest_payment_id()
        try:
            r = self.engine.pay_link(self.s, link["link_id"], phone)
        except StkUnconfirmed:
            pid = self._latest_payment_id()
            self.say("Engine", f"The STK request timed out, so the outcome is unknown. Payment #{pid} is kept as "
                               "'unconfirmed', not failed: the customer may still get the prompt (the link's use is kept too).", "warn", "stk")
            return pid
        except EngineError as e:
            self.say("Engine", f"Refused ({e.http_status}): {e.message}", "warn", "stk")
            return None
        assert self._latest_payment_id() != before
        p = self._p(r.payment_id)
        self.say("Engine", f"Normalises the phone to {mask_phone(p.phone)}, records payment #{p.id} as pending and "
                           f"sends the STK push ({p.checkout_request_id}).", "info", "stk")
        self.say("Customer", "Sees the M-Pesa prompt and enters their PIN.", "info", "safaricom")
        return p.id

    def _callback(self, pid, kind="success", new_cid=None):
        """Safaricom POSTs an STK result to the callback URL. kind: success|cancelled|wrong_amount|no_amount."""
        p = self._p(pid)
        cid = new_cid or p.checkout_request_id
        amount = {"success": p.amount, "wrong_amount": 1, "no_amount": None, "cancelled": None}[kind]
        code = 1032 if kind == "cancelled" else 0
        body = stk_callback_body(cid, amount, code, receipt=self._receipt(pid), phone=p.phone,
                                 desc="Request cancelled by user" if code else "The service request is processed successfully.")
        claim = "a cancellation" if code else (f"SUCCESS for {kes(amount)}" if amount is not None else "SUCCESS with no Amount")
        self._wire("←", "STK callback", f"{cid} claims {claim}", "info")
        self.say("Safaricom", f"POSTs {claim} to /callback/stk/<secret>.", "info", "callback")
        q0, ev0 = len(self.gateway.queries), len(self.events)
        self.engine.handle_stk_callback(self.s, body)
        if len(self.gateway.queries) > q0:
            self.say("Engine", "Does not trust the callback alone: asks Safaricom to confirm (STK query) → "
                               f"{self.gateway.last_query}.", "info", "verify")
        self._outcome(pid, ev0)

    def _outcome(self, pid, ev0=0):
        p = self._p(pid)
        if p.status == "SUCCESS":
            extra = ""
            if p.result_desc == "adopted_from_callback":
                extra = " (matched by phone + amount: adopted)"
            elif p.result_desc == "recovered_from_failed":
                extra = " (recovered)"
            self.say("Engine", f"Payment #{pid} → SUCCESS{extra}. Receipt: {p.mpesa_receipt or 'not known yet'}.", "ok", "success")
            fired = sum(1 for e in self.events if e["kind"] == "hook" and e["pid"] == pid and "succeeded" in e["text"])
            if fired:
                self.say("Your app", f"on_payment_succeeded ran {'once' if fired == 1 else f'{fired} times'}: mark the order paid here.", "ok" if fired == 1 else "bad", "hook")
            d = self._d(pid)
            if d:
                lvl = "ok" if d.status in ("pending", "sent") else "warn"
                self.say("Engine", f"Payout row claimed first, then sent: {kes(d.gross_amount)} − fee {kes(d.fee_amount)} = "
                                   f"{kes(d.net_amount)} to {d.destination_type} {d.destination_number} [{d.status}"
                                   f"{': ' + d.result_desc if d.status == 'failed' else ''}].", lvl, "payout")
            else:
                self.say("Engine", f"No payout row exists for payment #{pid}.", "warn", "payout")
        elif p.status == "FAILED":
            self.say("Engine", f"Payment #{pid} → FAILED ({p.result_desc}). Nothing credited, no payout.", "bad", "verify")
        elif p.status == "unconfirmed":
            self.say("Engine", f"Payment #{pid} stays unconfirmed: nothing matched it yet.", "warn", "verify")
        else:
            self.say("Engine", f"Payment #{pid} is still pending.", "info", "verify")
        for e in self.events[ev0:]:
            if e["kind"] == "alert":
                self.say("Engine", f"ALERT to a human: {e['text']}", "warn", "verify")

    def _b2b_result(self, pid, ok=True):
        d = self._d(pid)
        if not d or not d.conversation_id or d.status != "pending":
            self.say("Safaricom", "No pending payout with a conversation id to report on.", "warn", "result")
            return
        code = 0 if ok else 2001
        self._wire("←", "B2B result", f"{d.conversation_id} code={code}", "ok" if ok else "bad")
        self.say("Safaricom", f"Reports the payout result ({'success' if ok else 'failed'}) to /callback/b2b/<secret>.", "info", "callback")
        self.engine.handle_b2b_callback(self.s, b2b_result_body(d.conversation_id, code, receipt=f"B2B{pid:04d}Z"))
        d = self._d(pid)
        self.say("Engine", f"Matched by conversation id → payout is now {d.status}"
                           f"{' (receipt ' + d.mpesa_receipt + ')' if d.mpesa_receipt else ''}.",
                 "ok" if d.status == "sent" else "bad", "result")

    def _reconcile(self, intro="Runs the reconciliation job."):
        self.say("Reconciler", intro, "info", "reconciler")
        out = self.engine.reconcile(self.s)
        bits = []
        p = out["payments"]
        if p["checked"]:
            bits.append(f"checked {p['checked']} stale pending → {p['settled_success']} settled SUCCESS, "
                        f"{p['settled_failed']} FAILED, {p['still_unknown']} still unknown")
        if out["failed_rechecked"]["checked"]:
            bits.append(f"re-checked {out['failed_rechecked']['checked']} FAILED → {out['failed_rechecked']['recovered']} recovered")
        if out["missing_disbursements_created"]:
            bits.append(f"created {out['missing_disbursements_created']} missing payout(s)")
        if out["disbursements_retried"]:
            bits.append(f"retried {out['disbursements_retried']} failed payout(s)")
        self.say("Reconciler", "; ".join(bits) if bits else "Nothing it is allowed to fix automatically.",
                 "ok" if bits else "info", "reconciler")
        att = {k: v for k, v in find_needs_attention(self.engine, self.s).items() if v}
        if att:
            self.say("Reconciler", "Needs a human: " + ", ".join(f"{k.replace('_', ' ')} = {v}" for k, v in att.items()), "warn", "reconciler")
        return out

    def fast_forward(self, minutes):
        """Move every timestamp into the past, so time-based jobs see time passing."""
        with self.lock:
            delta = timedelta(minutes=minutes)
            for model in (Payment, Disbursement, MpesaCallback, PaymentLink):
                for row in self.s.execute(select(model)).scalars():
                    row.created_at = row.created_at - delta
                    if model is PaymentLink and row.expires_at:
                        row.expires_at = row.expires_at - delta
            self.s.commit()
        label = f"{minutes // 1440} day(s)" if minutes >= 1440 else f"{minutes} minutes"
        self.say("Operator", f"Time passes: +{label}.", "info", None)

    # ── scenarios ──────────────────────────────────────────────────────────
    def sc_happy(self):
        pid = self._start(1000)
        self._callback(pid)
        self._b2b_result(pid)

    def sc_duplicate(self):
        pid = self._start(1000)
        self._callback(pid)
        self.say("Safaricom", "Retries the same callback twice (it does this when it thinks we were slow).", "info", "callback")
        for _ in range(2):
            self._callback(pid)
        outcomes = [c["outcome"] for c in self.snapshot()["inbox"][:3]][::-1]
        self.say("Engine", f"Inbox outcomes: {', '.join(outcomes)}. B2B calls for this payment: "
                           f"{sum(1 for c in self.gateway.b2b if c[3] == f'PAY-{pid}')}.", "ok", "payout")

    def sc_wrong_amount(self):
        pid = self._start(100)
        self._callback(pid, "wrong_amount")

    def sc_forged_success(self):
        pid = self._start(100)
        self.gateway.query_mode = "failed"
        self.say("Simulator", "Safaricom will answer 'not paid' to any query.", "info", None)
        self._callback(pid, "success")
        self.gateway.query_mode = "success"

    def sc_forged_failure(self):
        pid = self._start(100)
        self.say("Simulator", "The customer really paid; Safaricom will confirm it.", "info", None)
        self._callback(pid, "cancelled")

    def sc_single_use(self):
        link = self._link(500)
        phone = self._phone()
        self.engine.pay_link(self.s, link["link_id"], phone)
        self.say("Engine", "First payer claims the link's only use and gets an STK push.", "ok", "stk")
        try:
            self.engine.pay_link(self.s, link["link_id"], self._phone())
        except EngineError as e:
            self.say("Engine", f"Second payer refused ({e.http_status}): {e.message}.", "ok", "stk")
        link2 = self._link(500)
        self.gateway.stk_mode = "reject"
        self.say("Simulator", "M-Pesa rejects the next STK push.", "info", None)
        try:
            self.engine.pay_link(self.s, link2["link_id"], self._phone())
        except EngineError as e:
            used = self.s.execute(select(PaymentLink.used_count).where(PaymentLink.link_id == link2["link_id"])
                                  .execution_options(populate_existing=True)).scalar()
            self.say("Engine", f"Push rejected ({e.http_status}). The payer was never charged, so the link's use is "
                               f"given back (used_count = {used}).", "ok", "stk")
        self.gateway.stk_mode = "accept"
        self.engine.pay_link(self.s, link2["link_id"], self._phone())
        self.say("Engine", "The same link works on the next attempt.", "ok", "stk")

    def sc_throttle(self):
        formats = ["0733111222", "254733111222", "+254733111222", "733111222", "0733 111 222"]
        limit = self.engine.settings.phone_throttle_limit
        for i, f in enumerate(formats):
            link = self.engine.create_link(self.s, self.MERCHANT_ID, amount=50)
            try:
                self.engine.pay_link(self.s, link["link_id"], f)
                self.say("Engine", f"Attempt {i + 1} typed as '{f}' → allowed ({i + 1}/{limit}).", "info", "stk")
            except EngineError as e:
                self.say("Engine", f"Attempt {i + 1} typed as '{f}' → blocked ({e.http_status}): {e.message}", "ok", "stk")
                break
        self.say("Engine", f"All formats collapse to {mask_phone('254733111222')} before counting, so formatting can't dodge the limit.", "ok", "stk")

    def sc_stk_timeout(self):
        self.gateway.stk_mode = "timeout"
        pid = self._start(750)
        self.gateway.stk_mode = "accept"
        self.say("Customer", "Pays anyway: the prompt did reach their phone.", "info", "safaricom")
        self._callback(pid, "success", new_cid=f"ws_CO_late_{pid}")

    def sc_lost_callback(self):
        pid = self._start(1200)
        self.say("Safaricom", "The callback never arrives (outage, firewall, bad URL).", "warn", "callback")
        self.fast_forward(10)
        self._reconcile("Wakes up and finds a payment pending for 10 minutes.")
        self.say("Safaricom", "Much later the real callback finally shows up.", "info", "callback")
        self._callback(pid)
        p = self._p(pid)
        self.say("Engine", f"Receipt backfilled: {p.mpesa_receipt}. B2B calls for this payment: "
                           f"{sum(1 for c in self.gateway.b2b if c[3] == f'PAY-{pid}')}.", "ok", "success")

    def sc_crash(self):
        self.payout_down = True
        pid = self._start(2000)
        self.say("Simulator", "The process dies right after SUCCESS is committed.", "info", None)
        self._callback(pid)
        self.payout_down = False
        self.fast_forward(5)
        self._reconcile("Restarts and looks for SUCCESS payments with no payout.")
        d = self._d(pid)
        self.say("Engine", f"Exactly one payout now exists for payment #{pid} [{d.status}]; B2B calls for it: "
                           f"{sum(1 for c in self.gateway.b2b if c[3] == f'PAY-{pid}')}.", "ok", "payout")

    def sc_b2b_timeout(self):
        self.gateway.b2b_mode = "timeout"
        pid = self._start(1000)
        self._callback(pid)
        self.gateway.b2b_mode = "accept"
        self.say("Engine", "The payout call timed out: Safaricom MAY have received it. The row is marked 'api_error' (ambiguous).", "warn", "payout")
        self.fast_forward(1440)
        self._reconcile("A day later it looks for failed payouts to retry.")
        self.say("Engine", f"Not retried: sending again could pay the merchant twice. B2B calls for this payment: "
                           f"{sum(1 for c in self.gateway.b2b if c[3] == f'PAY-{pid}')}. A human checks the Safaricom portal.", "ok", "payout")

    def sc_b2b_reject(self):
        self.gateway.b2b_mode = "reject"
        pid = self._start(1000)
        self._callback(pid)
        for _ in range(3):
            self.fast_forward(1440)
            self._reconcile("Retries payouts known not to have moved money.")
        d = self._d(pid)
        self.say("Engine", f"Stopped after {d.attempts} attempts (the limit): needs a human.", "ok", "payout")
        self.gateway.b2b_mode = "accept"
        self.say("Operator", "Fixes the initiator credentials in the Safaricom portal, then retries by hand.", "info", None)
        status = self.engine.retry_disbursement(self.s, d.id)
        self.say("Engine", f"Manual retry → payout is now {status}.", "ok" if status != "failed" else "bad", "payout")

    # ── public entry points: each returns the recorded steps ───────────────
    def _run(self, title, fn, *args):
        with self.lock:
            self.steps, self.lit = [], []
            if title:
                self.say("—", title, "title")
            try:
                fn(*args)
            except Exception as e:      # surface engine/lab bugs in the story instead of crashing the UI
                self.say("Lab", f"Unexpected error: {type(e).__name__}: {e}", "bad")
            return list(self.steps)

    def run_scenario(self, key):
        sc = next(s for s in SCENARIOS if s.key == key)
        return self._run(sc.title, getattr(self, f"sc_{key}"))

    def manual_pay(self, phone, amount):
        def go():
            try:
                r = self.engine.initiate_stk(self.s, self.MERCHANT_ID, phone, int(amount), reference=f"MAN-{self._phone_n}")
            except (EngineError, ValueError) as e:
                msg = getattr(e, "message", str(e))
                self.say("Engine", f"Refused ({getattr(e, 'http_status', 400)}): {msg}", "warn", "stk")
                return
            except StkUnconfirmed:
                raise
            p = self._p(r.payment_id)
            self.say("Engine", f"Payment #{p.id} pending, STK push sent to {mask_phone(p.phone)} ({p.checkout_request_id}).", "info", "stk")
        def safe():
            try:
                go()
            except StkUnconfirmed:
                self.say("Engine", f"STK outcome unknown: payment #{self._latest_payment_id()} kept as 'unconfirmed'.", "warn", "stk")
        return self._run("Manual: STK push", safe)

    def manual_callback(self, kind):
        def go():
            pid = self.s.execute(select(Payment.id).where(Payment.status.in_(("pending", "unconfirmed")))
                                 .order_by(desc(Payment.id)).limit(1)).scalar()
            if pid is None:
                self.say("Lab", "No pending payment. Send an STK push first.", "warn")
                return
            p = self._p(pid)
            self._callback(pid, kind, new_cid=f"ws_CO_late_{pid}" if not p.checkout_request_id else None)
        return self._run(f"Manual: deliver '{kind}' callback", go)

    def manual_b2b_result(self, ok):
        def go():
            did = self.s.execute(select(Disbursement.payment_id).where(Disbursement.status == "pending")
                                 .order_by(desc(Disbursement.id)).limit(1)).scalar()
            if did is None:
                self.say("Lab", "No payout is waiting for a result.", "warn")
                return
            self._b2b_result(did, ok)
        return self._run("Manual: payout result", go)

    def manual_reconcile(self):
        return self._run("Manual: run the reconciler", self._reconcile)

    def manual_fast_forward(self, minutes):
        return self._run(None, self.fast_forward, minutes)

    def set_mode(self, which, value):
        setattr(self.gateway, f"{which}_mode", value)
