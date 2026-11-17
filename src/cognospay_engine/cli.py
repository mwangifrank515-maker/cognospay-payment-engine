"""``cognospay-engine`` command line.

    cognospay-engine init-db   --database-url postgresql+psycopg2://...
    cognospay-engine reconcile --factory myapp.payments:build_engine [--loop 300]

``--factory`` is ``module:callable``; the callable returns ``(PaymentEngine, session_factory)``
so the reconciler uses YOUR payout resolver and hooks.
"""
import argparse
import importlib
import logging
import sys


def _load(spec: str):
    mod, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit("--factory must look like 'package.module:callable'")
    return getattr(importlib.import_module(mod), attr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cognospay-engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("init-db", help="create the engine tables (development; use Alembic in production)")
    p1.add_argument("--database-url", required=True)
    p2 = sub.add_parser("reconcile", help="run the reconciliation job")
    p2.add_argument("--factory", required=True)
    p2.add_argument("--loop", type=int, metavar="SECONDS", help="repeat forever every N seconds")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if a.cmd == "init-db":
        from sqlalchemy import create_engine

        from .models import Base
        Base.metadata.create_all(create_engine(a.database_url))
        print("tables created")
        return 0

    from .reconciliation import run_all, run_loop
    eng, session_factory = _load(a.factory)()
    if a.loop:
        run_loop(eng, session_factory, a.loop)
    with session_factory() as s:
        print(run_all(eng, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
