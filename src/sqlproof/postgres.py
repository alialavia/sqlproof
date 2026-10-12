"""Public access to the PostgreSQL connection adapter SqlProof uses internally.

Use this when a test or script needs a raw connection alongside SqlProof,
for example to apply DDL or seed rows before building a ``SqlProof``::

    from sqlproof import postgres

    with postgres.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE TABLE ...")

Parameters are inlined into the SQL text on the client by PyGreSQL, with
libpq escaping. Lists of numbers or booleans are sent as typed arrays;
other lists are sent as array literals whose type comes from the column
or an explicit cast, so write ``%s::uuid[]`` and the like when the
context does not supply one.
"""

from __future__ import annotations

from sqlproof._database import (
    Connection,
    Cursor,
    DatabaseError,
    DataError,
    Error,
    Json,
    Jsonb,
    connect,
    dict_row,
    sqlstate,
    tuple_row,
)
from sqlproof._range import Range

__all__ = [
    "Connection",
    "Cursor",
    "DataError",
    "DatabaseError",
    "Error",
    "Json",
    "Jsonb",
    "Range",
    "connect",
    "dict_row",
    "sqlstate",
    "tuple_row",
]
