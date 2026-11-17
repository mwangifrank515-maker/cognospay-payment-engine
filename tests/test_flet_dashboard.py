"""The Flet dashboard's lab drives the REAL engine: these tests pin what each scenario must prove."""
import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "examples" / "flet_dashboard"))
from lab import SCENARIOS, Lab  # noqa: E402


def texts(steps):
    return " | ".join(s.text for s in steps)


@pytest.fixture()
def lab():
    return Lab()


def one(lab, table):
    return lab.snapshot()[table][0]


def test_happy_path(lab):
    steps = lab.run_scenario("happy")
    snap = lab.snapshot()
    assert snap["counts"]["payments"]["SUCCESS"] == 1 and snap["counts"]["payouts"]["sent"] == 1
    assert (one(lab, "payouts")["gross"], one(lab, "payouts")["fee"], one(lab, "payouts")["net"]) == (1000, 20, 980)
    assert snap["b2b_calls"] == 1 and "ran once" in texts(steps)


def test_duplicate_callback_one_credit_one_payout(lab):
    lab.run_scenario("duplicate")
    snap = lab.snapshot()
    assert snap["b2b_calls"] == 1 and sum(1 for e in lab.events if e["kind"] == "hook") == 1
    assert [c["outcome"] for c in snap["inbox"][:3]][::-1] == ["applied", "duplicate", "duplicate"]


def test_wrong_amount_is_flagged_not_credited(lab):
    lab.run_scenario("wrong_amount")
    p = one(lab, "payments")
    assert (p["status"], p["note"]) == ("FAILED", "amount_mismatch") and lab.snapshot()["b2b_calls"] == 0
    assert lab.snapshot()["alerts"] == 1


def test_forged_success_denied_by_safaricom(lab):
    lab.run_scenario("forged_success")
    assert one(lab, "payments")["note"] == "verification_failed" and lab.snapshot()["b2b_calls"] == 0


def test_forged_failure_is_overruled_and_alerted(lab):
    steps = lab.run_scenario("forged_failure")
    assert one(lab, "payments")["status"] == "SUCCESS" and lab.snapshot()["b2b_calls"] == 1
    assert "contradicted" in texts(steps)


def test_single_use_link_and_giving_the_use_back(lab):
    steps = lab.run_scenario("single_use")
    t = texts(steps)
    assert "already been used" in t and "used_count = 0" in t and "works on the next attempt" in t


def test_throttle_collapses_formats_and_blocks_the_fourth(lab):
    steps = lab.run_scenario("throttle")
    assert lab.snapshot()["stk_calls"] == 3 and "blocked (429)" in texts(steps)


def test_stk_timeout_is_unconfirmed_then_adopted(lab):
    steps = lab.run_scenario("stk_timeout")
    p = one(lab, "payments")
    assert p["status"] == "SUCCESS" and p["note"] == "adopted_from_callback" and lab.snapshot()["b2b_calls"] == 1
    assert "unconfirmed" in texts(steps)


def test_lost_callback_settled_by_reconciler_and_receipt_backfilled(lab):
    lab.run_scenario("lost_callback")
    p = one(lab, "payments")
    assert p["status"] == "SUCCESS" and p["receipt"] and lab.snapshot()["b2b_calls"] == 1


def test_crash_before_payout_is_repaired_exactly_once(lab):
    steps = lab.run_scenario("crash")
    assert "No payout row exists" in texts(steps)
    snap = lab.snapshot()
    assert len(snap["payouts"]) == 1 and snap["b2b_calls"] == 1


def test_b2b_timeout_is_ambiguous_and_never_retried(lab):
    lab.run_scenario("b2b_timeout")
    snap = lab.snapshot()
    assert one(lab, "payouts")["note"] == "api_error" and snap["b2b_calls"] == 1
    assert snap["attention"]["payouts_ambiguous"] == 1


def test_b2b_reject_bounded_retries_then_manual_retry(lab):
    lab.run_scenario("b2b_reject")
    snap = lab.snapshot()
    assert snap["b2b_calls"] == 4                       # 3 automatic attempts, then 1 manual
    assert one(lab, "payouts")["status"] == "pending" and one(lab, "payouts")["attempts"] == 4


def test_every_scenario_runs_without_lab_errors_and_every_step_has_a_frame(lab):
    for sc in SCENARIOS:
        steps = lab.run_scenario(sc.key)
        assert steps and steps[0].level == "title"
        assert all(s.frame is not None for s in steps)
        assert "Unexpected error" not in texts(steps), sc.key
        assert steps[-1].frame["b2b_calls"] == lab.snapshot()["b2b_calls"]


def test_a_failing_engine_call_is_shown_in_the_story_not_raised(lab):
    lab.engine.handle_stk_callback = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    steps = lab.run_scenario("happy")
    assert any(s.level == "bad" and "boom" in s.text for s in steps)


def test_manual_flow(lab):
    lab.manual_pay("0712345678", "250")
    assert one(lab, "payments")["status"] == "pending"
    lab.manual_callback("success")
    assert one(lab, "payments")["status"] == "SUCCESS" and one(lab, "payouts")["status"] == "pending"
    lab.manual_b2b_result(True)
    assert one(lab, "payouts")["status"] == "sent"


def test_manual_errors_are_reported_not_raised(lab):
    assert "No pending payment" in texts(lab.manual_callback("success"))
    assert "Refused" in texts(lab.manual_pay("123", "100"))
    assert "Refused" in texts(lab.manual_pay("0712345678", "10.5"))
    assert "waiting for a result" in texts(lab.manual_b2b_result(True))


def test_modes_and_reset(lab):
    lab.set_mode("stk", "timeout")
    lab.manual_pay("0712345678", "100")
    assert one(lab, "payments")["status"] == "unconfirmed"
    lab.reset()
    assert lab.snapshot()["payments"] == [] and lab.gateway.stk_mode == "accept"


def test_fast_forward_ages_records(lab):
    lab.manual_pay("0712345678", "100")
    lab.manual_fast_forward(180)
    assert one(lab, "payments")["age"] == "3h"


# ── the Flet UI itself (skipped when flet isn't installed) ───────────────────
flet = pytest.importorskip("flet")


class FakePage:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


@pytest.fixture()
def dash():
    import app as dashboard_app
    d = dashboard_app.Dashboard(FakePage(), Lab())
    d.speed = "Instant"
    return d


def encode(control):
    """Serialise with Flet's own transport encoder: catches values the client could not receive."""
    import msgpack
    from flet.controls.base_control import BaseControl
    from flet.messaging.protocol import configure_encode_object_for_msgpack
    return msgpack.packb(control, default=configure_encode_object_for_msgpack(BaseControl), use_bin_type=True)


def test_ui_builds_and_encodes(dash):
    assert len(encode(dash.root)) > 10_000


def test_ui_plays_every_scenario_and_every_tab_renders(dash):
    import app as dashboard_app

    async def go():
        for sc in SCENARIOS:
            await dash.run_scenario(sc.key)
            encode(dash.root)
        for t in dashboard_app.TABS:
            dash.on_tab(t)
            encode(dash.root)
    asyncio.run(go())
    assert dash.frame == dash.lab.snapshot() and len(dash.story.controls) > 100 and dash.page.updates > 100
    assert not dash.busy


def test_ui_manual_controls_and_reset(dash):
    async def go():
        await dash.on_manual_pay(None)
        await dash.on_manual_callback(None)
        await dash.on_b2b_ok(None)
        await dash.on_reconcile(None)
        await dash.on_ff10(None)
        await dash.on_ff1d(None)
        for which, v in (("stk", "timeout"), ("query", "failed"), ("b2b", "reject")):
            dash.on_mode(which, v)
        dash.on_cb_kind("wrong_amount")
        encode(dash.root)
        dash.on_reset(None)
    asyncio.run(go())
    assert dash.lab.gateway.stk_mode == "accept" and dash.frame["payments"] == [] and len(dash.story.controls) == 1


def test_ui_ignores_clicks_while_a_scenario_is_playing(dash):
    dash.busy = True
    asyncio.run(dash.run_scenario("happy"))
    assert dash.frame["payments"] == [] and dash.lab.snapshot()["payments"] == []


def test_ui_icons_all_exist():
    import re

    import app as dashboard_app
    from lab import NODES
    src = pathlib.Path(dashboard_app.__file__).read_text()
    names = set(re.findall(r'icon\("([A-Z_]+)"', src)) | {n for _, _, n in NODES} | set(dashboard_app.ACTOR_ICON.values())
    names |= {i for _, i in dashboard_app.LEVEL.values()} | set(re.findall(r'button\("[^"]*", "([A-Z_]+)"', src))
    assert [n for n in names if not hasattr(flet.Icons, n)] == []
