import importlib.util
import pathlib
import sys

import pytest

pytest.importorskip("flask")
from cognospay_engine.testing import FakeGateway, stk_callback_body

EXAMPLE = pathlib.Path(__file__).parent.parent / "examples" / "flask_app.py"
SECRET = "flask-secret-flask-secret-123"


@pytest.fixture()
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/f.db")
    monkeypatch.setenv("MPESA_CALLBACK_SECRET", SECRET)
    spec = importlib.util.spec_from_file_location("flask_example", EXAMPLE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["flask_example"] = m
    spec.loader.exec_module(m)
    m.engine.gateway = FakeGateway()
    yield m
    sys.modules.pop("flask_example", None)


def test_flask_checkout_to_paid_order(mod):
    c = mod.app.test_client()
    pid = c.post("/checkout", json={"order_id": "ORD-1", "phone": "0712345678", "amount": 500}).get_json()["payment_id"]
    assert c.get(f"/checkout/{pid}").get_json() == {"status": "pending", "phone": "25471***78", "order": "awaiting_payment"}
    cid = mod.engine.gateway.stk_result["CheckoutRequestID"]
    assert c.post(f"/callback/stk/{SECRET}", json=stk_callback_body(cid, 500)).get_json()["ResultCode"] == 0
    assert c.get(f"/checkout/{pid}").get_json()["order"] == "paid" and not mod.engine.gateway.b2b   # collect-only


def test_flask_security_and_error_mapping(mod):
    c = mod.app.test_client()
    assert c.post("/callback/stk/wrong", json={}).status_code == 404
    assert c.post(f"/callback/stk/{SECRET}", data="not json").status_code == 200
    r = c.post("/checkout", json={"order_id": "X", "phone": "123", "amount": 10})
    assert r.status_code == 400 and "valid Kenyan" in r.get_json()["detail"]
    assert c.get("/checkout/999").status_code == 404
