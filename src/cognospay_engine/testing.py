"""Test helpers for code that integrates the engine: no network, no Safaricom.

    from cognospay_engine.testing import FakeGateway, stk_callback_body

    gw = FakeGateway()
    engine = PaymentEngine(gw, callback_secret="x" * 24)
    ...
    engine.handle_stk_callback(session, stk_callback_body(checkout_id, amount=100))
"""


class FakeGateway:
    """Stands in for :class:`~cognospay_engine.DarajaClient`. Every outbound call is recorded in
    ``stk`` / ``b2b`` / ``queries``; the answers are plain attributes you can script."""

    def __init__(self):
        self.stk, self.b2b, self.queries = [], [], []
        self.stk_result = {"CheckoutRequestID": "ws_CO_mock"}
        self.query_result = {"ResultCode": "0"}
        self.b2b_result = {"ResponseCode": "0", "ConversationID": "AG_1"}

    def stk_push(self, phone, amount, account_ref, transaction_desc):
        self.stk.append((phone, amount, account_ref))
        return self.stk_result

    def query_transaction(self, checkout_request_id):
        self.queries.append(checkout_request_id)
        return self.query_result

    def b2b_till(self, amount, till_number, account_ref):
        self.b2b.append(("till", amount, till_number, account_ref))
        return self.b2b_result

    def b2b_paybill(self, amount, destination_shortcode, account_ref):
        self.b2b.append(("paybill", amount, destination_shortcode, account_ref))
        return self.b2b_result


def stk_callback_body(checkout_request_id, amount=100, code=0, receipt="RCP123", phone=None,
                      desc="The service request is processed successfully."):
    """A Safaricom STK result in the exact shape Daraja posts. ``code != 0`` is a failure
    (1032 = cancelled by the user). ``amount=None`` omits the Amount item."""
    cb = {"CheckoutRequestID": checkout_request_id, "ResultCode": code, "ResultDesc": desc}
    if code == 0:
        items = [{"Name": "MpesaReceiptNumber", "Value": receipt}]
        if amount is not None:
            items.insert(0, {"Name": "Amount", "Value": amount})
        if phone:
            items.append({"Name": "PhoneNumber", "Value": int(phone)})
        cb["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": cb}}


def b2b_result_body(conversation_id, code=0, receipt="B2BRCP", desc="The service request is processed successfully."):
    """A Safaricom B2B result in the shape Daraja posts to the ResultURL."""
    return {"Result": {"ConversationID": conversation_id, "ResultCode": code, "ResultDesc": desc,
                       "ResultParameters": {"ResultParameter": [{"Key": "TransactionReceipt", "Value": receipt}]}}}
