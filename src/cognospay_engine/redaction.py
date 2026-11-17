"""Keep the M-Pesa callback secret out of logs.

The callback URL embeds the secret as a path segment (``/callback/stk/<secret>``). Any access
log that prints the request path would write the secret to disk on every callback. Call
:func:`install_callback_redaction` once at startup (and again after your server configures its
own loggers); it is idempotent.
"""
import logging
import re

_CALLBACK_PATH = re.compile(r"(/callback/[^/\s?\"']+/)[^/\s?\"']+")


def redact_callback_paths(text: str) -> str:
    return _CALLBACK_PATH.sub(r"\1[redacted]", text)


class RedactCallbackSecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(
                    redact_callback_paths(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: redact_callback_paths(v) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            if isinstance(record.msg, str):
                record.msg = redact_callback_paths(record.msg)
        except Exception:  # never let log redaction break a request
            pass
        return True


_FILTER = RedactCallbackSecretFilter()


def install_callback_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error", "gunicorn.access", "gunicorn.error", ""):
        lg = logging.getLogger(name)
        lg.addFilter(_FILTER)
        for h in lg.handlers:
            h.addFilter(_FILTER)
