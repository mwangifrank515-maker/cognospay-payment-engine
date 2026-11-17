# Contributing

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

Rules for changes to money-moving code:

1. Every safety rule in `docs/ARCHITECTURE.md` needs a test that **fails** if the rule is removed. Break the code on purpose and watch it fail before you trust the test.
2. A status change is a guarded `UPDATE … WHERE status = …`. Check `rowcount`.
3. Anything ambiguous (money may have moved) is reported to a human, never retried automatically.
4. No phone numbers, secrets or raw payloads in log lines.
