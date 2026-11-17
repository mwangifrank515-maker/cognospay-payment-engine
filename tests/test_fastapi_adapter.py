import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cognospay_engine.contrib.fastapi import create_callback_router, register_error_handlers
from conftest import SECRET, make_link, make_payment, pay, stk_body


@pytest.fixture()
def client(eng, sf):
    def get_db():
        with sf() as s:
            yield s
    app = FastAPI()
    app.include_router(create_callback_router(eng, get_db))
    register_error_handlers(app)

    @app.post("/pay/{link_id}")
    def pay_endpoint(link_id: str, body: dict, db=Depends(get_db)):
        return {"payment_id": eng.pay_link(db, link_id, body.get("phone"), body.get("amount")).payment_id}
    return TestClient(app)


def test_wrong_secret_is_a_404_and_nothing_is_processed(client, db, gw):
    pid, cid = make_payment(db, 100)
    assert client.post("/callback/stk/wrong", json=stk_body(cid, 100)).status_code == 404
    assert pay(db, pid).status == "pending" and not gw.queries


def test_stk_callback_end_to_end_in_a_worker_thread(client, db, gw):
    pid, cid = make_payment(db, 100)
    r = client.post(f"/callback/stk/{SECRET}", json=stk_body(cid, 100))
    assert r.status_code == 200 and r.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert pay(db, pid).status == "SUCCESS" and len(gw.b2b) == 1


def test_non_json_and_malformed_bodies_are_accepted_not_500(client):
    assert client.post(f"/callback/stk/{SECRET}", content=b"not json").status_code == 200
    assert client.post(f"/callback/b2b/{SECRET}", json={"x": 1}).status_code == 200
    assert client.post(f"/callback/b2b-timeout/{SECRET}", content=b"").status_code == 200


def test_b2b_routes_apply_results(client, db, gw):
    pid, cid = make_payment(db, 100)
    client.post(f"/callback/stk/{SECRET}", json=stk_body(cid, 100))
    client.post(f"/callback/b2b/{SECRET}", json={"Result": {"ConversationID": "AG_1", "ResultCode": 0, "ResultDesc": "ok"}})
    from conftest import disb
    assert disb(db, pid).status == "sent"


def test_engine_errors_map_to_http_status(client, db):
    lid = make_link(db, 100)
    assert client.post(f"/pay/{lid}", json={"phone": "123"}).status_code == 400
    assert client.post("/pay/unknown", json={"phone": "0712345678"}).status_code == 404
    assert client.post(f"/pay/{lid}", json={"phone": "0712345678"}).status_code == 200
    r = client.post(f"/pay/{lid}", json={"phone": "0712345678"})
    assert r.status_code == 410 and "already been used" in r.json()["detail"]
