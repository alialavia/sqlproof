"""`scale_analysis(in_clone=True)`: the sweep runs in a throwaway copy, so
the proof's own tables are untouched (scale follow-up 9)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof import _dsn as conninfo
from sqlproof._dsn import quote_identifier
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale import scale_analysis

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


@pytest.fixture
def seeded() -> Iterator[str]:
    """A database of its own (a clone needs a template nobody else is on),
    whose tables already hold rows."""
    base = os.environ[DSN_ENV]
    name = f"sqlproof_clone_src_{uuid4().hex[:8]}"
    with database.connect(base, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {quote_identifier(name)}")
    dsn = conninfo.make_conninfo(base, dbname=name)
    try:
        with database.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE items (id bigint PRIMARY KEY, v int NOT NULL);"
                "INSERT INTO items SELECT g, g FROM generate_series(1, 7) g;"
                "CREATE FUNCTION total() RETURNS bigint LANGUAGE sql STABLE AS "
                "$$ SELECT sum(v) FROM items $$"
            )
        yield dsn
    finally:
        with database.connect(base, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE)")


def test_a_sweep_in_a_clone_leaves_the_source_alone(seeded: str) -> None:
    proof = SqlProof.from_config(SqlProofConfig(connection_string=seeded, schema="public"))
    with pytest.raises(SqlProofUsageError, match="in_clone=True"):
        scale_analysis(proof, "public.total", sizes={"items": 300}, artifact_dir=None)

    result = scale_analysis(
        proof,
        "public.total",
        sizes={"items": 300},
        max_factor=16,
        artifact_dir=None,
        in_clone=True,
    )
    assert len(result.points) >= 5  # it measured, in the clone
    with database.connect(seeded) as conn:
        rows = conn.execute("SELECT count(*), sum(v) FROM items").fetchone()
        clones = conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE 'sqlproof_scale_%'"
        ).fetchone()
    assert tuple(rows) == (7, 28)
    assert clones[0] == 0
