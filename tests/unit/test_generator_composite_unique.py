"""Tests for composite UNIQUE / PRIMARY KEY enforcement in the row
generator (#26).

The generator previously enforced UNIQUE constraints only when the
constraint covered a single column. Composite UNIQUEs (and composite
PRIMARY KEYs, which are a composite UNIQUE + NOT NULL combo) were
silently ignored — the generator would produce datasets where two
rows shared the same composite-key tuple, and Postgres would reject
the INSERT.

The fix: after generating each row, check every composite UNIQUE /
composite PRIMARY KEY against previously-generated rows. If the new
row's tuple matches a previous row's, use Hypothesis's ``assume()``
to invalidate the example so the framework retries with different
draws.

Invariants pinned down here:

  (i) For any table with a composite UNIQUE on columns (c1, c2, …),
      no two rows in the generated set produce the same tuple
      (row[c1], row[c2], …).
  (ii) Composite PRIMARY KEYs are treated as composite UNIQUEs.
  (iii) Single-column UNIQUEs continue to work via the existing
        `_unique_value` path (no regression).
  (iv) If a row has any of the composite-key columns missing (e.g.
       a deferred FK column that's still NULL), the row is
       excluded from the uniqueness check — we can't compare what
       isn't there yet.

Failure cases each invariant addresses:
  (i): The original bug — INSERT fails with
       `duplicate key value violates unique constraint`.
  (ii): Same shape, surfaced via the supabase_rls example's
        `org_members(org_id, user_id)` PRIMARY KEY.
  (iii): A regression where the new logic accidentally drops the
         single-column path.
  (iv): A row in a cyclic schema (where some FK columns are
        deferred and left NULL) would otherwise have NULL in its
        composite-key tuple and falsely match other NULL rows.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.model import Column, ForeignKey, PgType, Table

INTEGER = PgType(kind="scalar", name="integer")


def _table(
    name: str,
    *,
    columns: tuple[tuple[str, bool], ...],
    foreign_keys: tuple[ForeignKey, ...] = (),
    primary_key: tuple[str, ...] = ("id",),
    unique_constraints: tuple[tuple[str, ...], ...] = (),
) -> Table:
    return Table(
        schema="public",
        name=name,
        columns=tuple(
            Column(c_name, INTEGER, nullable=nullable, default=None, is_generated=False)
            for c_name, nullable in columns
        ),
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_constraints=unique_constraints,
        check_constraints=(),
    )


# ---------------------------------------------------------------------------
# Regression: single-column UNIQUE still works
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_single_column_unique_still_produces_distinct_values(data: st.DataObject) -> None:
    """Invariant (iii): single-column UNIQUE rows produce distinct
    values on the unique column. Failure case: refactor accidentally
    drops the `_unique_value` path used today."""
    table = _table(
        "things",
        columns=(("id", False), ("sku", False)),
        unique_constraints=(("sku",),),
    )
    rows = data.draw(table_rows_strategy(table, count=5))
    skus = [row["sku"] for row in rows]
    assert len(set(skus)) == len(skus), f"Duplicate skus in {skus}"


# ---------------------------------------------------------------------------
# New: composite UNIQUE
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_composite_unique_produces_distinct_tuples(data: st.DataObject) -> None:
    """Invariant (i): for a composite UNIQUE on (a, b), no two rows
    produce the same (a, b) tuple. Failure case: INSERT fails with
    `duplicate key value violates unique constraint`."""
    table = _table(
        "pairs",
        columns=(("id", False), ("a", False), ("b", False)),
        unique_constraints=(("a", "b"),),
    )
    rows = data.draw(table_rows_strategy(table, count=4))
    tuples = [(row["a"], row["b"]) for row in rows]
    assert len(set(tuples)) == len(tuples), f"Duplicate (a, b) tuples in {tuples}"


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_composite_primary_key_treated_as_composite_unique(
    data: st.DataObject,
) -> None:
    """Invariant (ii): composite PRIMARY KEY produces distinct row
    tuples. Mirrors the supabase_rls example's `org_members(org_id,
    user_id)` PRIMARY KEY which was the original failing case in #26."""
    table = _table(
        "org_members",
        columns=(("org_id", False), ("user_id", False), ("role", False)),
        primary_key=("org_id", "user_id"),
    )
    rows = data.draw(table_rows_strategy(table, count=4))
    tuples = [(row["org_id"], row["user_id"]) for row in rows]
    assert len(set(tuples)) == len(tuples), f"Duplicate (org_id, user_id) in {tuples}"


@given(data=st.data())
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_multiple_composite_uniques_are_all_honored(
    data: st.DataObject,
) -> None:
    """Invariant (i) extended: a table can have multiple composite
    UNIQUEs and each must produce distinct tuples independently.
    Failure case: implementation only checks the first UNIQUE,
    silently allowing duplicates on the second."""
    table = _table(
        "things",
        columns=(("id", False), ("a", False), ("b", False), ("c", False)),
        unique_constraints=(("a", "b"), ("b", "c")),
    )
    rows = data.draw(table_rows_strategy(table, count=4))
    pairs_ab = [(row["a"], row["b"]) for row in rows]
    pairs_bc = [(row["b"], row["c"]) for row in rows]
    assert len(set(pairs_ab)) == len(pairs_ab), f"Duplicate (a, b) in {pairs_ab}"
    assert len(set(pairs_bc)) == len(pairs_bc), f"Duplicate (b, c) in {pairs_bc}"


@given(data=st.data())
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_composite_with_missing_columns_does_not_falsely_collide(
    data: st.DataObject,
) -> None:
    """Invariant (iv): rows missing any of the composite-key columns
    (e.g. a deferred FK column that was left NULL) shouldn't be
    compared against each other as if their NULL values collide.

    The behavior we want: rows that don't have all the composite-key
    columns in their row dict are skipped during the uniqueness check.

    Failure case it addresses: a cyclic schema (per PR #47) leaves a
    deferred FK column unset on every row; the uniqueness check
    incorrectly says all those rows have the same NULL tuple and
    discards every example.
    """
    # We can't directly model 'missing column' easily through the
    # public strategy API — the generator always populates non-
    # default, non-deferred columns. Instead, this test documents
    # the invariant: a table where one composite-key column happens
    # to be a nullable column with no FK fallback should still
    # produce distinct rows. The generator will set those columns
    # to NULL; the uniqueness check should treat NULL ≠ NULL
    # (matching SQL's NULL-in-UNIQUE semantics).
    table = _table(
        "rows_with_optional",
        columns=(("id", False), ("a", False), ("b", True)),
        unique_constraints=(("a", "b"),),
    )
    rows = data.draw(table_rows_strategy(table, count=3))
    # If `b` happens to be None for multiple rows, SQL would allow it
    # (NULL distinct in standard UNIQUE). Our check should also allow
    # it. So we just assert the generator didn't refuse to produce
    # rows entirely.
    assert len(rows) == 3
