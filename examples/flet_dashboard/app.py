"""CognosPay Engine Lab: a Flet dashboard that shows how the payment engine works.

    pip install "cognospay-engine[demo]"
    python examples/flet_dashboard/app.py            # desktop window
    flet run --web examples/flet_dashboard/app.py    # or in a browser

Everything on screen is the REAL engine running against an in-memory database and a simulated
Safaricom, so no credentials and no network are needed. Pick a scenario on the left and watch the
payment travel through the pipeline while the tables update step by step. Written for Flet 0.86.
"""
import asyncio
import os
import sys

import flet as ft

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lab import ATTENTION_HELP, NODES, PAYMENT_STATUSES, PAYOUT_STATUSES, SCENARIOS, Lab, kes  # noqa: E402

# ── palette: same navy + gold as the CognosPay app ─────────────────────────
BG_APP, BG_SIDEBAR, BG_CARD, BG_INPUT = "#0B1829", "#0D1E35", "#112236", "#0D1E35"
BORDER, GOLD, GOLD_LIGHT, ON_GOLD = "#1E3A58", "#C9A84C", "#E2C97E", "#0D1205"
TEXT, MUTED = "#EAF0F8", "#6B88A8"
OK, WARN, BAD, INFO = "#3DDC97", "#F5A623", "#FF6B6B", "#5BA7F7"

STATUS_COLOR = {"pending": INFO, "unconfirmed": WARN, "SUCCESS": OK, "FAILED": BAD,
                "initiating": WARN, "sent": OK, "failed": BAD}
LEVEL = {"info": (INFO, "INFO"), "ok": (OK, "CHECK_CIRCLE"), "warn": (WARN, "WARNING"), "bad": (BAD, "CANCEL")}
ACTOR_ICON = {"Your app": "EXTENSION", "Customer": "PERSON", "Safaricom": "CLOUD", "Engine": "SETTINGS",
              "Reconciler": "AUTORENEW", "Operator": "ENGINEERING", "Simulator": "SCIENCE", "Lab": "BUG_REPORT"}
TAG_COLOR = {"Happy path": OK, "Attack": BAD, "Failure": WARN, "Recovery": INFO}
SPEEDS = {"Instant": 0, "Fast": 0.25, "Normal": 0.7, "Slow": 1.4}
TABS = ["Payments", "Payouts", "Callback inbox", "Safaricom wire", "Hooks & alerts", "Needs attention"]


# ── tiny widget kit ────────────────────────────────────────────────────────
def alpha(color, a=0.14):
    return ft.Colors.with_opacity(a, color)


def icon(name, color=TEXT, size=18):
    return ft.Icon(getattr(ft.Icons, name, ft.Icons.CIRCLE), color=color, size=size)


def txt(value, size=13, color=TEXT, bold=False, **kw):
    return ft.Text(str(value), size=size, color=color, weight=ft.FontWeight.BOLD if bold else None, **kw)


def pill(label, color):
    return ft.Container(content=txt(label, 11, color, True), border_radius=20, bgcolor=alpha(color, 0.15),
                        padding=ft.Padding(left=9, top=3, right=9, bottom=3),
                        border=ft.Border.all(1, alpha(color, 0.45)))


def card(content, padding=16, **kw):
    return ft.Container(content=content, padding=padding, bgcolor=BG_CARD, border_radius=14,
                        border=ft.Border.all(1, BORDER), **kw)


def button(label, icon_name, on_click, primary=False, expand=None):
    fg = ON_GOLD if primary else TEXT
    return ft.Button(
        content=ft.Row([icon(icon_name, fg, 16), txt(label, 12, fg, True)], spacing=6, tight=True),
        on_click=on_click, expand=expand,
        style=ft.ButtonStyle(bgcolor=GOLD if primary else BG_SIDEBAR,
                             side=None if primary else ft.BorderSide(1, BORDER),
                             shape=ft.RoundedRectangleBorder(radius=8),
                             padding=ft.Padding(left=12, top=9, right=12, bottom=9)))


def segmented(options, value, on_pick):
    """A row of mutually exclusive pills. ``options`` is a list of (value, label)."""
    items = []
    for v, label in options:
        on = v == value
        items.append(ft.Container(
            content=txt(label, 12, ON_GOLD if on else TEXT, on), bgcolor=GOLD if on else BG_SIDEBAR,
            border=ft.Border.all(1, GOLD if on else BORDER), border_radius=8,
            padding=ft.Padding(left=10, top=6, right=10, bottom=6),
            on_click=(lambda e, v=v: on_pick(v))))
    return ft.Row(items, spacing=6, wrap=True, run_spacing=6)


def grid(cols, rows, height=260, empty="Nothing here yet. Run a scenario."):
    """Fixed-height scrolling table. ``cols`` = [(title, width|None)]; cells are str or Control."""
    def cell(c, w):
        body = c if isinstance(c, ft.Control) else txt(c, 12, TEXT, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)
        return ft.Container(content=body, width=w, expand=True if w is None else None)

    head = ft.Row([cell(txt(t, 11, MUTED, True), w) for t, w in cols], spacing=8)
    if not rows:
        return ft.Column([head, ft.Divider(height=1, color=BORDER), ft.Container(txt(empty, 12, MUTED), padding=14)])
    body = ft.Column(
        [ft.Container(ft.Row([cell(c, w) for c, (_, w) in zip(r, cols, strict=True)], spacing=8,
                             vertical_alignment=ft.CrossAxisAlignment.CENTER),
                      padding=ft.Padding(left=0, top=6, right=0, bottom=6),
                      border=ft.Border(bottom=ft.BorderSide(1, alpha(BORDER, 0.6)))) for r in rows],
        scroll=ft.ScrollMode.AUTO, spacing=0, height=height)
    return ft.Column([head, ft.Divider(height=1, color=BORDER), body], spacing=4)


# ── the dashboard ──────────────────────────────────────────────────────────
class Dashboard:
    def __init__(self, page, lab: Lab):
        self.page, self.lab = page, lab
        self.speed, self.tab, self.busy = "Normal", "Payments", False
        self.cb_kind, self.lit = "success", []
        self.phone = ft.TextField(label="Phone", value="0712345678", width=150, dense=True, text_size=13,
                                  color=TEXT, bgcolor=BG_INPUT, border_color=BORDER, border_radius=8)
        self.amount = ft.TextField(label="KES", value="100", width=90, dense=True, text_size=13,
                                   color=TEXT, bgcolor=BG_INPUT, border_color=BORDER, border_radius=8)
        self.frame = lab.snapshot()

        self.metrics = ft.Container()
        self.pipeline = ft.Container()
        self.statuses = ft.Container()
        self.tab_bar = ft.Row(spacing=4)
        self.table = ft.Container()
        self.seg = {k: ft.Container() for k in ("stk", "query", "b2b", "cb")}
        self.sim = self._build_sim()
        self.speed_box = ft.Container()
        self.story = ft.ListView(spacing=10, auto_scroll=True, height=340, padding=ft.Padding(left=2, top=2, right=6, bottom=2))
        self._empty_story()
        self.root = self._layout()
        self.render()

    # ── layout ─────────────────────────────────────────────────────────────
    def _layout(self):
        header = ft.Container(
            padding=ft.Padding(left=24, top=14, right=24, bottom=14), bgcolor=BG_SIDEBAR,
            border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Row([
                icon("SHIELD", GOLD, 26),
                ft.Column([txt("CognosPay Engine Lab", 19, TEXT, True),
                           txt("The real engine, a simulated Safaricom, an in-memory database. No credentials, no network.", 11, MUTED)],
                          spacing=0, expand=True),
                self.speed_box,
                button("Reset lab", "RESTART_ALT", self.on_reset),
            ], spacing=18, vertical_alignment=ft.CrossAxisAlignment.CENTER))

        scenarios = ft.ListView([self._scenario_card(s) for s in SCENARIOS], spacing=10, expand=True)
        left = ft.Container(
            width=350, bgcolor=BG_SIDEBAR, padding=16, border=ft.Border(right=ft.BorderSide(1, BORDER)),
            content=ft.Column([txt("Scenarios", 15, GOLD_LIGHT, True),
                               txt("Each one runs the real engine and replays what happened.", 11, MUTED),
                               scenarios], spacing=8, expand=True))

        story_card = card(ft.Column([
            ft.Row([txt("What is happening", 14, GOLD_LIGHT, True), ft.Container(expand=True),
                    ft.TextButton(content=txt("Clear", 11, MUTED), on_click=self.on_clear)]),
            ft.Container(self.story, height=340)], spacing=6), expand=True)
        sim_card = card(self.sim, width=370)
        data_card = card(ft.Column([self.tab_bar, self.table], spacing=10))

        right = ft.Container(expand=True, padding=20, content=ft.Column([
            self.metrics,
            card(ft.Column([txt("How a payment moves", 14, GOLD_LIGHT, True), self.pipeline], spacing=10)),
            self.statuses,
            ft.Row([story_card, sim_card], spacing=16, vertical_alignment=ft.CrossAxisAlignment.START),
            data_card,
        ], scroll=ft.ScrollMode.AUTO, spacing=16, expand=True))
        return ft.Column([header, ft.Row([left, right], spacing=0, expand=True,
                                         vertical_alignment=ft.CrossAxisAlignment.STRETCH)], spacing=0, expand=True)

    def _scenario_card(self, sc):
        async def go(e):
            await self.run_scenario(sc.key)
        return ft.Container(
            padding=14, bgcolor=BG_CARD, border_radius=12, border=ft.Border.all(1, BORDER), on_click=go,
            content=ft.Column([
                ft.Row([txt(sc.title, 13, TEXT, True, expand=True), pill(sc.tag, TAG_COLOR[sc.tag])]),
                txt(sc.blurb, 11, MUTED),
            ], spacing=6))

    def _empty_story(self):
        self.story.controls = [ft.Container(padding=10, content=ft.Column([
            txt("Pick a scenario on the left.", 13, TEXT, True),
            txt("The payment travels along the pipeline above while the tables below update step by step. "
                "Nothing is scripted: each line is read back from what the engine actually did.", 12, MUTED),
        ], spacing=4))]
        self._story_empty = True

    # ── rendering ──────────────────────────────────────────────────────────
    def render(self):
        f = self.frame
        self.metrics.content = ft.Row([
            self._stat("STK pushes sent", f["stk_calls"], "to customers' phones", INFO),
            self._stat("Payments SUCCESS", f["counts"]["payments"]["SUCCESS"], "verified with Safaricom", OK),
            self._stat("B2B payouts sent", f["b2b_calls"], "real money to merchants", GOLD),
            self._stat("Alerts raised", f["alerts"], "things a human must see", WARN),
            self._stat("Needs attention", sum(f["attention"].values()), "reported, never auto-fixed", BAD),
        ], spacing=12)
        self.pipeline.content = self._pipeline()
        self.statuses.content = ft.Row([
            card(self._status_group("Payment status", PAYMENT_STATUSES, f["counts"]["payments"]), expand=True),
            card(self._status_group("Payout status", PAYOUT_STATUSES, f["counts"]["payouts"]), expand=True)], spacing=16)
        self._update_sim()
        self.speed_box.content = ft.Row([txt("Playback", 11, MUTED), segmented(
            [(k, k) for k in SPEEDS], self.speed, self.on_speed)], spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self._tabs()

    def _stat(self, label, value, hint, color):
        return card(ft.Column([txt(label, 11, MUTED), txt(value, 28, color, True), txt(hint, 10, MUTED)], spacing=0),
                    padding=14, expand=True)

    def _pipeline(self):
        parts = []
        for i, (key, label, ico) in enumerate(NODES):
            lit, latest = key in self.lit, bool(self.lit) and key == self.lit[-1]
            if key == "reconciler":
                parts.append(ft.Container(width=1, height=54, bgcolor=BORDER, margin=ft.Margin(left=8, top=0, right=8, bottom=0)))
            parts.append(ft.Container(
                width=102, padding=ft.Padding(left=6, top=10, right=6, bottom=10), border_radius=12,
                bgcolor=alpha(GOLD, 0.16) if latest else (alpha(GOLD, 0.07) if lit else BG_SIDEBAR),
                border=ft.Border.all(2 if latest else 1, GOLD if latest else (alpha(GOLD, 0.5) if lit else BORDER)),
                content=ft.Column([icon(ico, GOLD if lit else MUTED, 22),
                                   txt(label, 11, TEXT if lit else MUTED, lit, text_align=ft.TextAlign.CENTER)],
                                  spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER)))
            if i < len(NODES) - 2:
                parts.append(icon("ARROW_FORWARD", alpha(MUTED, 0.8), 16))
        parts.append(txt("← safety net", 10, MUTED))
        return ft.Column([ft.Row(parts, wrap=True, run_spacing=10, spacing=4,
                                 vertical_alignment=ft.CrossAxisAlignment.CENTER),
                          txt("Lit boxes are the stages the last scenario touched; the brightest is where it is now.", 11, MUTED)],
                         spacing=8)

    def _status_group(self, title, statuses, counts):
        chips = [ft.Container(
            expand=True, padding=ft.Padding(left=10, top=8, right=10, bottom=8), border_radius=10,
            bgcolor=alpha(STATUS_COLOR[s], 0.14 if counts.get(s) else 0.04),
            border=ft.Border.all(1, alpha(STATUS_COLOR[s], 0.6 if counts.get(s) else 0.2)),
            content=ft.Column([txt(counts.get(s, 0), 20, STATUS_COLOR[s] if counts.get(s) else MUTED, True),
                               txt(s, 11, TEXT if counts.get(s) else MUTED)], spacing=0)) for s in statuses]
        return ft.Column([txt(title, 12, MUTED, True), ft.Row(chips, spacing=8)], spacing=8)

    def _build_sim(self):
        """Static controls are created once (so Flet keeps typed text); only the pill rows are refreshed."""
        return ft.Column([
            txt("Safaricom simulator", 14, GOLD_LIGHT, True),
            txt("Decide how the fake Safaricom answers, then try things yourself.", 11, MUTED),
            txt("STK push answer", 11, MUTED, True), self.seg["stk"],
            txt("STK query answer (verification)", 11, MUTED, True), self.seg["query"],
            txt("B2B payout answer", 11, MUTED, True), self.seg["b2b"],
            ft.Divider(height=8, color=BORDER),
            txt("Do it yourself", 12, MUTED, True),
            ft.Row([self.phone, self.amount], spacing=8),
            button("Send STK push", "SEND", self.on_manual_pay, primary=True),
            self.seg["cb"],
            button("Deliver callback to latest pending", "WEBHOOK", self.on_manual_callback),
            ft.Row([button("Payout ✓", "DONE_ALL", self.on_b2b_ok), button("Payout ✗", "CANCEL", self.on_b2b_bad)], spacing=8),
            ft.Row([button("Run reconciler", "AUTORENEW", self.on_reconcile),
                    button("+10 min", "SCHEDULE", self.on_ff10), button("+1 day", "FAST_FORWARD", self.on_ff1d)],
                   spacing=8, wrap=True, run_spacing=8),
        ], spacing=8)

    def _update_sim(self):
        g = self.lab.gateway
        pick = lambda which: (lambda v: self.on_mode(which, v))  # noqa: E731
        self.seg["stk"].content = segmented([("accept", "Accept"), ("timeout", "Time out"), ("reject", "Reject")],
                                            g.stk_mode, pick("stk"))
        self.seg["query"].content = segmented([("success", "Paid"), ("failed", "Not paid"), ("unknown", "No answer")],
                                              g.query_mode, pick("query"))
        self.seg["b2b"].content = segmented([("accept", "Accept"), ("reject", "Reject"), ("timeout", "Time out")],
                                            g.b2b_mode, pick("b2b"))
        self.seg["cb"].content = segmented([("success", "Success"), ("cancelled", "Cancelled"),
                                            ("wrong_amount", "Wrong amt"), ("no_amount", "No amt")],
                                           self.cb_kind, self.on_cb_kind)

    def _tabs(self):
        f = self.frame
        needs = sum(1 for v in f["attention"].values() if v)
        counts = {"Payments": len(f["payments"]), "Payouts": len(f["payouts"]), "Callback inbox": len(f["inbox"]),
                  "Safaricom wire": len(f["wire"]), "Hooks & alerts": len(f["events"]), "Needs attention": needs}
        btns = []
        for t in TABS:
            on = t == self.tab
            btns.append(ft.Container(
                padding=ft.Padding(left=12, top=8, right=12, bottom=8), border_radius=8,
                bgcolor=alpha(GOLD, 0.14) if on else None,
                border=ft.Border.all(1, GOLD if on else "#00000000"),
                on_click=lambda e, t=t: self.on_tab(t),
                content=ft.Row([txt(t, 12, GOLD_LIGHT if on else MUTED, on),
                                pill(counts[t], BAD if t == "Needs attention" and counts[t] else MUTED)], spacing=6)))
        self.tab_bar.controls = btns
        self.table.content = {
            "Payments": self._payments, "Payouts": self._payouts, "Callback inbox": self._inbox,
            "Safaricom wire": self._wire, "Hooks & alerts": self._events, "Needs attention": self._attention,
        }[self.tab]()

    def _payments(self):
        rows = [[str(p["id"]), pill(p["status"], STATUS_COLOR.get(p["status"], MUTED)), kes(p["amount"]), p["phone"],
                 p["receipt"] or "–", p["note"] or "–", p["cid"] or "–", p["age"]] for p in self.frame["payments"]]
        return grid([("#", 36), ("Status", 120), ("Amount", 90), ("Phone", 110), ("Receipt", 110),
                     ("Reason / note", None), ("Checkout ID", 130), ("Age", 44)], rows)

    def _payouts(self):
        rows = [[str(d["id"]), f"pay #{d['payment_id']}", kes(d["gross"]), kes(d["fee"]), kes(d["net"]), d["dest"],
                 pill(d["status"], STATUS_COLOR.get(d["status"], MUTED)), str(d["attempts"]), d["note"] or "–"]
                for d in self.frame["payouts"]]
        return grid([("#", 36), ("Payment", 70), ("Gross", 80), ("Fee", 70), ("Net", 80), ("Destination", 120),
                     ("Status", 110), ("Tries", 44), ("Note", None)], rows)

    def _inbox(self):
        col = {"applied": OK, "duplicate": INFO, "unmatched": BAD, "adopted": GOLD, "left_pending": WARN,
               "recovered": GOLD, "ignored": MUTED, "orphan": BAD, "received": MUTED}
        rows = [[str(c["id"]), c["kind"], c["ref"] or "–", c["code"] or "–", pill(c["outcome"], col.get(c["outcome"], MUTED))]
                for c in self.frame["inbox"]]
        return grid([("#", 36), ("Kind", 90), ("Reference", 190), ("Code", 50), ("Outcome", None)], rows,
                    empty="Every raw callback is stored here before anything acts on it.")

    def _wire(self):
        col = {"info": TEXT, "ok": OK, "warn": WARN, "bad": BAD}
        rows = [[w["t"], txt(w["dir"], 14, GOLD if w["dir"] == "→" else INFO, True), w["kind"],
                 txt(w["text"], 12, col.get(w["level"], TEXT), no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)]
                for w in reversed(self.frame["wire"])]
        return grid([("Time", 70), ("", 26), ("Call", 100), ("Detail", None)], rows,
                    empty="Calls to and from Safaricom appear here. → is the engine calling out, ← is a callback arriving.")

    def _events(self):
        rows = [[e["t"], pill("ALERT" if e["kind"] == "alert" else "HOOK", WARN if e["kind"] == "alert" else OK), e["text"]]
                for e in reversed(self.frame["events"])]
        return grid([("Time", 70), ("Kind", 80), ("What happened", None)], rows,
                    empty="Hooks you register (mark order paid) and alerts for a human show up here.")

    def _attention(self):
        items = [(k, v) for k, v in self.frame["attention"].items() if v]
        if not items:
            return ft.Container(padding=14, content=ft.Row([icon("CHECK_CIRCLE", OK, 20),
                                                            txt("All clear. Nothing needs a human right now.", 13, OK)], spacing=8))
        return ft.Column([ft.Container(
            padding=12, border_radius=10, bgcolor=alpha(BAD, 0.08), border=ft.Border.all(1, alpha(BAD, 0.4)),
            content=ft.Row([pill(v, BAD), ft.Column([txt(k.replace("_", " "), 13, TEXT, True), txt(ATTENTION_HELP[k], 11, MUTED)],
                                                    spacing=1, expand=True)], spacing=12)) for k, v in items], spacing=8)

    def _step_view(self, st):
        if st.level == "title":
            return ft.Container(
                padding=ft.Padding(left=0, top=6, right=0, bottom=2), content=ft.Column([
                    txt(st.text, 14, GOLD, True), ft.Divider(height=1, color=alpha(GOLD, 0.4))], spacing=4))
        color, ico = LEVEL.get(st.level, (INFO, "INFO"))
        sim = st.actor == "Simulator"
        return ft.Row([
            ft.Container(width=30, height=30, border_radius=15, bgcolor=alpha(color, 0.16),
                         alignment=ft.alignment.Alignment(0, 0), content=icon(ico, color, 16)),
            ft.Column([
                ft.Row([icon(ACTOR_ICON.get(st.actor, "CIRCLE"), MUTED, 12), txt(st.actor, 11, MUTED, True)], spacing=4),
                txt(st.text, 12, MUTED if sim else TEXT, italic=sim)], spacing=1, expand=True),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.START)

    # ── playback ───────────────────────────────────────────────────────────
    async def play(self, steps):
        delay = SPEEDS[self.speed]
        self.busy = True
        try:
            for st in steps:
                if self._story_empty:
                    self.story.controls, self._story_empty = [], False
                self.frame = st.frame or self.frame
                if st.node:
                    if st.node in self.lit:
                        self.lit.remove(st.node)
                    self.lit.append(st.node)
                self.story.controls.append(self._step_view(st))
                self.render()
                self.page.update()
                if delay:
                    await asyncio.sleep(delay)
            self.frame = self.lab.snapshot()
            self.render()
            self.page.update()
        finally:
            self.busy = False

    async def _do(self, fn, *args, reset_lit=True):
        if self.busy:
            return
        if reset_lit:
            self.lit = []
        await self.play(fn(*args))

    async def run_scenario(self, key):
        await self._do(self.lab.run_scenario, key)

    # ── handlers ───────────────────────────────────────────────────────────
    def _refresh(self):
        self.frame = self.lab.snapshot()
        self.render()
        self.page.update()

    def on_tab(self, t):
        self.tab = t
        self._tabs()
        self.page.update()

    def on_speed(self, v):
        self.speed = v
        self.render()
        self.page.update()

    def on_mode(self, which, v):
        self.lab.set_mode(which, v)
        self._refresh()

    def on_cb_kind(self, v):
        self.cb_kind = v
        self._refresh()

    async def on_manual_pay(self, e):
        await self._do(self.lab.manual_pay, self.phone.value, self.amount.value)

    async def on_manual_callback(self, e):
        await self._do(self.lab.manual_callback, self.cb_kind, reset_lit=False)

    async def on_b2b_ok(self, e):
        await self._do(self.lab.manual_b2b_result, True, reset_lit=False)

    async def on_b2b_bad(self, e):
        await self._do(self.lab.manual_b2b_result, False, reset_lit=False)

    async def on_reconcile(self, e):
        await self._do(self.lab.manual_reconcile, reset_lit=False)

    async def on_ff10(self, e):
        await self._do(self.lab.manual_fast_forward, 10, reset_lit=False)

    async def on_ff1d(self, e):
        await self._do(self.lab.manual_fast_forward, 1440, reset_lit=False)

    def on_clear(self, e):
        self._empty_story()
        self.page.update()

    def on_reset(self, e):
        if self.busy:
            return
        self.lab.reset()
        self.lit = []
        self._empty_story()
        self._refresh()


def main(page: ft.Page):
    page.title = "CognosPay Engine Lab"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG_APP
    page.padding = 0
    try:
        page.window.width, page.window.height = 1480, 940
        page.window.min_width, page.window.min_height = 1180, 700
    except Exception:
        pass            # web / mobile: no window
    page.add(Dashboard(page, Lab()).root)


if __name__ == "__main__":
    ft.run(main)
