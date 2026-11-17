"""Shared column types.

``Money`` is stored as exact fixed-point NUMERIC in the database (no binary-float drift in
SUM()/comparisons) while Python keeps seeing plain floats, on Postgres AND SQLite.
"""
from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator


class _FloatNumeric(TypeDecorator):
    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int):
        super().__init__(precision, scale, asdecimal=False)
        self._p, self._s = precision, scale

    def process_result_value(self, value, dialect):
        return None if value is None else float(value)

    def copy(self, **kw):
        return _FloatNumeric(self._p, self._s)


# KES amounts: up to 9,999,999,999.99
Money = _FloatNumeric(12, 2)
