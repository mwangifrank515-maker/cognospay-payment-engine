"""The shipped FastAPI example must keep working: import it with a fake Safaricom and drive it."""
import importlib.util
import pathlib
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from cognospay_engine.testing import FakeGateway, stk_callback_body

EXAMPLE = pathlib.Path(__file__).parent.parent / "examples" / "fastapi_app.py"


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("MPESA_CALLBACK_SECRET", "example-secret-example-secret")
    monkeypatch.setenv("MPESA_CONSUMER_KEY", "k")
    spec = importlib.util.spec_from_file_location("example_app", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["example_app"] = mod
    spec.loader.exec_module(mod)
    mod.engine.gateway = FakeGateway()
    yield mod
    sys.modules.pop("example_app", None)


def test_example_app_full_flow(app_module):
    c = TestClient(app_module.app)
    h = {"x-api-key": "dev-key"}
    assert c.post("/links", json={"amount": 100}).status_code in (401, 422)       # auth required
    link = c.post("/links", json={"amount": 100, "description": "demo"}, headers=h).json()
    pid = c.post(f"/pay/{link['link_id']}", json={"phone": "0712345678"}).json()["payment_id"]
    assert c.get(f"/payments/{pid}", headers=h).json()["status"] == "pending"

    cid = app_module.engine.gateway.stk_result["CheckoutRequestID"]
    cb = c.post("/callback/stk/example-secret-example-secret", json=stk_callback_body(cid, 100, receipt="EX1"))
    assert cb.status_code == 200
    st = c.get(f"/payments/{pid}", headers=h).json()
    assert st["status"] == "SUCCESS" and st["receipt"] == "EX1" and st["phone"] == "25471***78"
    assert len(app_module.engine.gateway.b2b) == 1
    assert c.post(f"/pay/{link['link_id']}", json={"phone": "0712345678"}).status_code == 410
    assert c.get("/payments/999", headers=h).status_code == 404
