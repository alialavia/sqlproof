"""Integration tests for FK cycle resolution (#47).

Unit tests in `tests/unit/test_dependency_graph_cycles.py` cover the
algorithm: which edges are deferred, what order tables appear in.
This file verifies the algorithm round-trips correctly against a
real Postgres — i.e. the INSERT-with-NULL-then-UPDATE pass in
`_insert_dataset` actually produces valid rows that satisfy the FK
constraints at commit time.

The shape exercised is the "current-version pointer" pattern from
the issue: a content table with a nullable current-version FK and
a versions table that NOT-NULL-references back. Plus a self-
referential `parent_id` on content, to exercise both cycle shapes
in one test.

Gated on `SQLPROOF_TEST_DATABASE_URL` like the rest of
`tests/integration/`. Skips locally when no DSN is set.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


SCHEMA_SQL = """
CREATE TABLE content (
  id UUID PRIMARY KEY,
  parent_id UUID REFERENCES content(id),
  current_version_id UUID
);

CREATE TABLE versions (
  id UUID PRIMARY KEY,
  content_id UUID NOT NULL REFERENCES content(id)
);

ALTER TABLE content
  ADD CONSTRAINT content_current_version_fk
  FOREIGN KEY (current_version_id) REFERENCES versions(id);
"""


@pytest.fixture
def cyclic_schema(request: pytest.FixtureRequest) -> tuple[str, str]:
    """Per-test schema with the FK-cycle shape from #47.

    Returns (dsn, schema_name) where dsn is unchanged from the
    environment but schema_name is unique to this test so concurrent
    runs don't collide. Tears down the schema on exit even if the
    test raises.
    """
    dsn = os.environ[DSN_ENV]
    schema_name = f"sqlproof_fk_cycle_it_{uuid4().hex[:12]}"

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            # Apply the cyclic schema inside the new schema. The
            # search_path setting scopes CREATE TABLE / ALTER TABLE
            # to this schema only.
            conn.execute(f'SET search_path TO "{schema_name}"')
            conn.execute(SCHEMA_SQL)
        except Exception:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            raise

    def cleanup() -> None:
        with psycopg.connect(dsn, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')

    request.addfinalizer(cleanup)
    return dsn, schema_name


def test_client_for_dataset_empty_dataset_no_longer_raises_on_cycle_schema(
    cyclic_schema: tuple[str, str],
) -> None:
    """Regression for the original #22 + #47 case: opening a client
    against a schema with FK cycles, with an empty dataset, must not
    raise — the cycle has nothing to resolve.

    Failure case it addresses: a Supabase project with the current-
    version-pointer pattern can't even open a test client because
    `insertion_order` raises before the empty-dataset short-circuit
    in `_insert_dataset` gets a chance to run.
    """
    dsn, schema_name = cyclic_schema
    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=schema_name))

    # Empty dataset on a cyclic schema: this used to raise
    # CircularDependencyError, blocking every property test in the
    # project (see issue #47).
    with proof.client_for_dataset({}) as db:
        # Nothing to assert beyond "the client opened without raising".
        # Run a trivial query to confirm we have a live session.
        assert db.scalar("SELECT 1") == 1


@pytest.mark.parametrize(
    ("n_content", "n_versions"),
    [(1, 1), (1, 3), (3, 1), (3, 3), (5, 2)],
    ids=["minimal", "more_versions", "more_content", "balanced", "asymmetric"],
)
def test_generated_dataset_round_trips_through_cycle_schema(
    cyclic_schema: tuple[str, str],
    n_content: int,
    n_versions: int,
) -> None:
    """Property test: for any small dataset size, generating and
    INSERTing rows against the cyclic schema must succeed AND
    produce rows where every FK is satisfied at commit time.

    Invariants checked:

      a) Every content row inserts without violating any FK.
      b) Every versions row inserts without violating its
         NOT-NULL content_id FK.
      c) After the UPDATE pass, every content row either has
         current_version_id NULL or pointing at a real versions row
         (the FK is satisfied).
      d) No `content.parent_id` value references a non-existent
         `content.id` (self-reference cycle handled).

    Failure cases this catches:
      - Inserter generates SQL that NOT-NULL-violates because it
        included a deferred column in the INSERT statement.
      - UPDATE pass references a versions row that doesn't exist
        because rows were ordered wrong.
      - Self-reference column tries to point at a row that hasn't
        been inserted yet (wrong intra-table order).
    """
    dsn, schema_name = cyclic_schema
    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=schema_name))

    # Schema-qualified queries because `SqlProofConfig.schema` doesn't
    # set search_path on the runtime connection (sqlproof reads the
    # schema for introspection but queries during property execution
    # default to whatever search_path the database session inherits).
    q_content = f'"{schema_name}".content'
    q_versions = f'"{schema_name}".versions'

    def property_succeeds(db) -> None:
        # Invariants (a, b, c, d). If any FK was violated, we wouldn't
        # have reached this point — the inserter would have raised
        # psycopg.errors.ForeignKeyViolation or NotNullViolation.
        n_content_actual = db.scalar(f"SELECT count(*) FROM {q_content}")
        n_versions_actual = db.scalar(f"SELECT count(*) FROM {q_versions}")
        assert n_content_actual == n_content
        assert n_versions_actual == n_versions

        # Invariant (c): current_version_id is NULL or refers to a real row.
        rows = db.query(
            f"SELECT c.current_version_id "
            f"FROM {q_content} c "
            f"LEFT JOIN {q_versions} v ON v.id = c.current_version_id "
            f"WHERE c.current_version_id IS NOT NULL AND v.id IS NULL"
        )
        assert rows == [], f"current_version_id references missing rows: {rows}"

        # Invariant (d): parent_id is NULL or refers to a real row.
        rows = db.query(
            f"SELECT c.parent_id "
            f"FROM {q_content} c "
            f"LEFT JOIN {q_content} p ON p.id = c.parent_id "
            f"WHERE c.parent_id IS NOT NULL AND p.id IS NULL"
        )
        assert rows == [], f"parent_id references missing rows: {rows}"

    proof.check(
        "cyclic_schema_round_trip",
        sizes={"content": n_content, "versions": n_versions},
        property=property_succeeds,
        runs=1,
    )
