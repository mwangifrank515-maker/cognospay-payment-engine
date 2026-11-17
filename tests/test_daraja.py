import requests

from cognospay_engine import DarajaClient, DarajaConfig

CFG = DarajaConfig("key", "secret", "174379", "passkey", "https://pay.example.com", "s" * 30,
                   initiator_name="api", security_credential="cred")


class Resp:
    def __init__(self, status=200, body=None, text=None, bad_json=False):
        self.status_code, self._body, self.bad_json = status, body, bad_json
        self.text = text if text is not None else str(body)

    def json(self):
        if self.bad_json:
            raise ValueError("no json")
        return self._body


class FakeHttp:
    def __init__(self, posts=(), gets=None):
        self.posts, self.gets, self.post_calls, self.get_calls = list(posts), gets, [], 0

    def get(self, url, **kw):
        self.get_calls += 1
        return self.gets() if callable(self.gets) else Resp(200, {"access_token": f"tok{self.get_calls}", "expires_in": "3599"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append((url, json, headers))
        r = self.posts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_token_cached_across_calls_and_refetched_once_on_401():
    http = FakeHttp(posts=[Resp(200, {"CheckoutRequestID": "a"}), Resp(401, {}), Resp(200, {"CheckoutRequestID": "b"})])
    c = DarajaClient(CFG, http=http)
    assert c.stk_push("254712345678", 10, "r", "d")["CheckoutRequestID"] == "a"
    assert http.get_calls == 1                                   # one OAuth fetch for the first call
    assert c.stk_push("254712345678", 10, "r", "d")["CheckoutRequestID"] == "b"
    assert http.get_calls == 2                                   # 401 -> dropped cache -> refetched ONCE
    assert http.post_calls[2][2]["Authorization"] == "Bearer tok2"


def test_stk_push_distinguishes_not_sent_unknown_and_rejected():
    no_token = DarajaClient(CFG, http=FakeHttp(gets=lambda: Resp(500, {})))
    assert no_token.stk_push("254712345678", 10, "r", "d") is None                       # nothing sent

    net = DarajaClient(CFG, http=FakeHttp(posts=[requests.ConnectionError()]))
    assert net.stk_push("254712345678", 10, "r", "d") == {"_unknown": True, "reason": "network"}

    bad5 = DarajaClient(CFG, http=FakeHttp(posts=[Resp(502, None, "<html>", bad_json=True)]))
    assert bad5.stk_push("254712345678", 10, "r", "d")["_unknown"] is True              # may have been sent

    bad4 = DarajaClient(CFG, http=FakeHttp(posts=[Resp(400, None, "nope", bad_json=True)]))
    assert bad4.stk_push("254712345678", 10, "r", "d") is None                           # clearly rejected

    rej = DarajaClient(CFG, http=FakeHttp(posts=[Resp(400, {"errorCode": "400.002.02", "errorMessage": "bad"})]))
    assert rej.stk_push("254712345678", 10, "r", "d")["errorCode"] == "400.002.02"       # explicit rejection

    five = DarajaClient(CFG, http=FakeHttp(posts=[Resp(503, {"msg": "x"})]))
    assert five.stk_push("254712345678", 10, "r", "d")["_unknown"] is True


def test_b2b_without_token_is_a_definite_failure_and_network_error_is_ambiguous():
    c = DarajaClient(CFG, http=FakeHttp(gets=lambda: Resp(500, {})))
    r = c.b2b_till(98, "555111", "PAY-1")
    assert r["_not_sent"] is True and r["ResponseCode"] == "-1"
    c2 = DarajaClient(CFG, http=FakeHttp(posts=[requests.ConnectionError()]))
    assert c2.b2b_till(98, "555111", "PAY-1") is None


def test_payload_shape_urls_and_truncation():
    http = FakeHttp(posts=[Resp(200, {"CheckoutRequestID": "a"}), Resp(200, {"ResponseCode": "0"})])
    c = DarajaClient(CFG, http=http)
    c.stk_push("254712345678", 99, "INV-2026-000123456", "A very long description")
    _, p, _ = http.post_calls[0]
    assert p["Amount"] == 99 and p["PartyB"] == "174379" and p["TransactionType"] == "CustomerPayBillOnline"
    assert len(p["AccountReference"]) <= 12 and len(p["TransactionDesc"]) <= 13
    assert p["CallBackURL"] == "https://pay.example.com/callback/stk/" + "s" * 30
    c.b2b_paybill(98, "600111", "PAY-1")
    _, b, _ = http.post_calls[1]
    assert b["CommandID"] == "BusinessPayBill" and b["RecieverIdentifierType"] == "4"
    assert b["ResultURL"].endswith("/callback/b2b/" + "s" * 30)
    assert b["QueueTimeOutURL"].endswith("/callback/b2b-timeout/" + "s" * 30)   # separate path


def test_instances_do_not_share_token_cache():
    h1, h2 = FakeHttp(posts=[Resp(200, {"CheckoutRequestID": "a"})]), FakeHttp(posts=[Resp(200, {"CheckoutRequestID": "b"})])
    DarajaClient(CFG, http=h1).stk_push("254712345678", 1, "r", "d")
    DarajaClient(CFG, http=h2).stk_push("254712345678", 1, "r", "d")
    assert h1.get_calls == 1 and h2.get_calls == 1
