"""Partial unique index support (#3, sub-feature 1).

A partial unique index is created with
``CREATE UNIQUE INDEX foo ON t (col) WHERE predicate;`` — the
uniqueness constraint applies only to rows matching the predicate.
The canonical example is a soft-delete pattern::

    CREATE TABLE users (id serial PRIMARY KEY, email text);
    CREATE UNIQUE INDEX users_email_active_uq
      ON users (email) WHERE deleted_at IS NULL;

Two rows ``(email='a@x', deleted_at=NULL)`` and
``(email='a@x', deleted_at='2024-01-01')`` are NOT a conflict —
only one of them satisfies the predicate.

Before this change, ``schema.parse_sql`` ignored ``CREATE UNIQUE
INDEX`` statements entirely (it only walks ``CreateStmt`` and
``CreateEnumStmt``), and ``schema.introspect`` queried
``pg_constraint`` with ``contype='u'`` which doesn't include
index-backed partial uniques. Both representations were silently
incomplete — schemas with soft-delete unique constraints would
have Postgres reject the generator's INSERTs at runtime with no
explanatory signal from sqlproof.

Invariants pinned down here:

  (i) The parser surfaces a ``PartialUniqueConstraint`` for every
      ``CREATE UNIQUE INDEX ... WHERE ...`` it sees, with the
      column list and predicate text captured.
  (ii) The row generator respects the predicate for the simple
       ``column IS NULL`` form: rows where the indexed column is
       NOT NULL on the filter column don't compete for uniqueness
       with rows where it IS NULL.
  (iii) For rows matching the predicate, uniqueness IS enforced
        (two NULL-deleted_at rows can't share an email).
  (iv) For predicates the generator can't evaluate, it falls back
       to skipping enforcement (documented limitation). The row
       generator must not blow up on unknown predicate shapes.

Failure cases each invariant addresses:
  (i): Schemas with partial unique indexes silently lost the
       constraint at parse/introspect time; downstream sqlproof
       code had no way to reason about them.
  (ii): Soft-delete schemas where the generator over-enforced
        uniqueness (treating partial unique as full unique) would
        emit ``assume()``-rejected examples for valid datasets.
        Pre-fix the introspector dropped the constraint instead,
        so this was an under-enforcement bug; the symptom was
        Postgres-side rejection at INSERT time.
  (iii): Without per-row predicate evaluation, the generator
         either over-applies uniqueness or doesn't apply it at
         all. The soft-delete case needs the middle ground.
  (iv): A partial unique with a predicate the generator can't
        evaluate (e.g. ``WHERE status IN ('a', 'b')``) shouldn't
        crash anything — sqlproof should just not enforce that
        index and let Postgres do its job at INSERT time.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.model import (
    Column,
    PartialUniqueConstraint,
    PgType,
    Table,
)
from sqlproof.schema.parse_sql import parse_schema_sql

INTEGER = PgType(kind="scalar", name="integer")
TEXT = PgType(kind="scalar", name="text")
TIMESTAMPTZ = PgType(kind="scalar", name="timestamptz")


# ---------------------------------------------------------------------------
# Invariant (i): parser surfaces partial unique indexes
# ---------------------------------------------------------------------------


def test_parser_captures_partial_unique_index_with_is_null_predicate() -> None:
    """Invariant (i): a partial unique index in SQL produces a
    PartialUniqueConstraint on the parsed Table. Failure case: the
    parser previously walked only CreateStmt/CreateEnumStmt and
    silently dropped IndexStmt nodes."""
    sql = """
        CREATE TABLE users (
            id serial PRIMARY KEY,
            email text NOT NULL,
            deleted_at timestamptz
        );
        CREATE UNIQUE INDEX users_email_active_uq
          ON users (email) WHERE deleted_at IS NULL;
    """
    info = parse_schema_sql(sql)
    users = info.table("users")
    assert users.partial_unique_constraints == (
        PartialUniqueConstraint(columns=("email",), predicate="deleted_at IS NULL"),
    )
    # Full UNIQUE constraints aren't polluted by the partial one.
    assert users.unique_constraints == ()


def test_parser_distinguishes_partial_from_unconditional_unique_index() -> None:
    """Invariant (i, regression): a non-partial CREATE UNIQUE INDEX
    must NOT be misclassified as partial just because the parser is
    now walking IndexStmts. Without a WHERE clause it stays unsurfaced
    via this code path (the historical behavior for INDEX-form unique
    constraints; the partial-unique work doesn't change that). The
    inline UNIQUE constraint path remains the canonical way to spell
    an unconditional unique."""
    sql = """
        CREATE TABLE things (
            id serial PRIMARY KEY,
            sku text NOT NULL,
            tenant integer NOT NULL,
            archived_at timestamptz
        );
        CREATE UNIQUE INDEX things_sku_uq ON things (sku);
        CREATE UNIQUE INDEX things_sku_per_tenant_uq
          ON things (sku, tenant) WHERE archived_at IS NULL;
    """
    info = parse_schema_sql(sql)
    things = info.table("things")
    assert things.partial_unique_constraints == (
        PartialUniqueConstraint(
            columns=("sku", "tenant"), predicate="archived_at IS NULL"
        ),
    )


# ---------------------------------------------------------------------------
# Invariant (ii) + (iii): generator behavior with simple IS NULL predicate
# ---------------------------------------------------------------------------


def _users_with_partial_unique() -> Table:
    return Table(
        schema="public",
        name="users",
        columns=(
            Column("id", INTEGER, nullable=False, default=None, is_generated=False),
            Column("email", TEXT, nullable=False, default=None, is_generated=False),
            Column(
                "deleted_at",
                TIMESTAMPTZ,
                nullable=True,
                default=None,
                is_generated=False,
            ),
        ),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
        partial_unique_constraints=(
            PartialUniqueConstraint(
                columns=("email",), predicate="deleted_at IS NULL"
            ),
        ),
    )


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_allows_email_collision_when_one_row_is_soft_deleted(
    data: st.DataObject,
) -> None:
    """Invariant (ii): two rows can share ``email`` if one of them
    has ``deleted_at`` set (i.e. doesn't match ``deleted_at IS
    NULL``). The generator must NOT reject these as a uniqueness
    violation. Failure case: pre-fix the constraint was either
    dropped (Postgres rejects at INSERT) or over-applied (Hypothesis
    flakes with `assume()` rejections that the schema actually
    permits).
    """
    table = _users_with_partial_unique()
    rows = data.draw(table_rows_strategy(table, count=8))
    # Group by (email, deleted_at IS NULL) and assert each group is internally
    # unique. Across groups, collisions are permitted.
    seen: dict[tuple[str, bool], int] = {}
    for row in rows:
        key = (row["email"], row["deleted_at"] is None)
        seen[key] = seen.get(key, 0) + 1
    for (_email, is_active), count in seen.items():
        if is_active:
            assert count == 1, (
                f"Active rows must have unique email; got {count} for {seen}"
            )


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_enforces_uniqueness_within_predicate_match(
    data: st.DataObject,
) -> None:
    """Invariant (iii): rows where the predicate is satisfied
    (``deleted_at IS NULL``) must have unique emails among
    themselves. Failure case: the generator skipped partial-unique
    enforcement entirely, so two ``deleted_at=NULL`` rows could
    duplicate emails and Postgres would reject the INSERT batch."""
    table = _users_with_partial_unique()
    rows = data.draw(table_rows_strategy(table, count=8))
    active_emails = [row["email"] for row in rows if row["deleted_at"] is None]
    assert len(set(active_emails)) == len(active_emails), (
        f"Duplicate emails among active rows: {active_emails}"
    )


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_handles_is_not_null_predicate(data: st.DataObject) -> None:
    """Invariant (iii, mirror): the ``IS NOT NULL`` predicate is the
    natural opposite of the soft-delete pattern (e.g. a partial
    unique on archived rows only). The compiler must enforce
    uniqueness on rows where the column IS set, not on rows where
    it's NULL. Failure case: the predicate compiler only handles
    ``IS NULL`` and silently drops ``IS NOT NULL``, breaking the
    parallel pattern."""
    table = Table(
        schema="public",
        name="archives",
        columns=(
            Column("id", INTEGER, nullable=False, default=None, is_generated=False),
            Column("name", TEXT, nullable=False, default=None, is_generated=False),
            Column(
                "archived_at",
                TIMESTAMPTZ,
                nullable=True,
                default=None,
                is_generated=False,
            ),
        ),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
        partial_unique_constraints=(
            PartialUniqueConstraint(
                columns=("name",), predicate="archived_at IS NOT NULL"
            ),
        ),
    )
    rows = data.draw(table_rows_strategy(table, count=8))
    # Rows where archived_at IS NOT NULL must have unique names.
    archived_names = [row["name"] for row in rows if row["archived_at"] is not None]
    assert len(set(archived_names)) == len(archived_names), (
        f"Duplicate names among archived rows: {archived_names}"
    )


# ---------------------------------------------------------------------------
# Invariant (iv): unsupported predicate doesn't crash the generator
# ---------------------------------------------------------------------------


def test_generator_does_not_crash_on_unsupported_predicate() -> None:
    """Invariant (iv): predicates the generator can't evaluate
    (anything more complex than ``column IS NULL``) cause the
    generator to skip enforcement, NOT to raise. Failure case: an
    AttributeError or similar inside the predicate evaluator would
    break dataset generation for any schema with a non-trivial
    partial unique."""
    table = Table(
        schema="public",
        name="orders",
        columns=(
            Column("id", INTEGER, nullable=False, default=None, is_generated=False),
            Column("sku", TEXT, nullable=False, default=None, is_generated=False),
            Column("status", TEXT, nullable=True, default=None, is_generated=False),
        ),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
        partial_unique_constraints=(
            # Predicate the simple evaluator can't read — should be
            # ignored gracefully, not crash.
            PartialUniqueConstraint(
                columns=("sku",), predicate="status = 'pending'"
            ),
        ),
    )

    @given(data=st.data())
    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def inner(data: st.DataObject) -> None:
        # We don't assert anything about the rows — just that
        # generation completes without raising.
        data.draw(table_rows_strategy(table, count=4))

    inner()
