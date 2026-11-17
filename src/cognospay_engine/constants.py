# A payout whose result_desc is one of these MAY already have moved money: never auto-retry,
# and a human must confirm before a manual retry.
AMBIGUOUS_DESCS = ("api_error", "initiating", "queue_timeout")

# FAILED payments with these reasons were put there on purpose for a human; never auto-recover.
DELIBERATE_FAIL_DESCS = ("amount_mismatch", "amount_missing", "verification_failed")

ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}   # what to answer Safaricom
