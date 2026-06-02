"""Exclusion constraint support (#3, sub-feature 3).

Exclusion constraints generalize UNIQUE: instead of equality on
every column, the user picks an operator per column and the
constraint rejects any pair of rows where ALL operators evaluate
true on the (row_a.col, row_b.col) pairs.

The canonical use case is room booking with overlap detection::

    CREATE TABLE bookings (
        id serial PRIMARY KEY,
        room integer,
        during tsrange,
        EXCLUDE USING gist (room WITH =, during WITH &&)
    );

This rejects two rows that have the same ``room`` AND overlapping
``during``. Two rows in different rooms are fine. Two rows in the
same room with non-overlapping times are fine.

Before this change the parser and introspector both dropped
exclusion constraints silently. The row generator emitted
unconstrained datasets and Postgres rejected the INSERT batch
whenever a conflict happened to arise.

Invariants pinned down here:

  (i) The parser surfaces every ``EXCLUDE`` clause as an
      ``ExclusionConstraint`` with the (column, operator) pairs
      and the access method preserved.
  (ii) When EVERY operator is ``=``, the constraint degrades to a
       composite UNIQUE and the generator enforces uniqueness on
       the column tuple. This is the most common shape after the
       overlap pattern and is mechanically the same as composite
       UNIQUE.
  (iii) When operators include non-``=`` forms (e.g. ``&&``), the
        generator does NOT enforce the constraint — full
        enforcement requires range-type support (#4b). The
        generator must NOT crash on these schemas; it must just
        produce rows and let Postgres reject conflicts at INSERT
        time.

Failure cases each invariant addresses:
  (i): Agents inspecting the schema (CLI / MCP) had no way to
       know an exclusion constraint existed. Tests written
       against the schema model couldn't reason about
       exclusion-protected invariants.
  (ii): All-equality exclusion constraints exist in real schemas
        (rarely used as a primary uniqueness constraint, but used
        with WHERE predicates) — without surfacing them as
        composite UNIQUEs the generator routinely produces
        duplicates and Postgres rejects the batch.
  (iii): Without graceful degradation, a single bookings-style
         table with an overlap exclusion would crash the entire
         generator path for the schema, even for unrelated tables
         in the same schema.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.model import ExclusionConstraint
from sqlproof.schema.parse_sql import parse_schema_sql

# ---------------------------------------------------------------------------
# Invariant (i): parser surfaces exclusion constraints
# ---------------------------------------------------------------------------


def test_parser_captures_exclusion_constraint() -> None:
    """Invariant (i): an EXCLUDE clause is surfaced as an
    ExclusionConstraint with the operator/column pairs intact.
    Failure case: the parser previously walked Constraint nodes
    only for PRIMARY/UNIQUE/FOREIGN/CHECK — EXCLUSION fell off."""
    sql = """
        CREATE TABLE bookings (
            id serial PRIMARY KEY,
            room integer NOT NULL,
            during tsrange NOT NULL,
            EXCLUDE USING gist (room WITH =, during WITH &&)
        );
    """
    info = parse_schema_sql(sql)
    bookings = info.table("bookings")
    assert bookings.exclusion_constraints == (
        ExclusionConstraint(
            columns_with_operators=(("room", "="), ("during", "&&")),
            access_method="gist",
        ),
    )


# ---------------------------------------------------------------------------
# Invariant (ii): all-equality exclusion degrades to composite UNIQUE
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_enforces_uniqueness_for_all_equality_exclusion(
    data: st.DataObject,
) -> None:
    """Invariant (ii): when every operator is ``=``, no two rows
    may share the same (col_a, col_b, ...) tuple. The constraint
    is mechanically a composite UNIQUE. Failure case: agents
    relying on exclusion-style uniqueness (e.g. a tenant-id /
    name pair excluded from collision) got duplicates in
    generated batches before this fix."""
    sql = """
        CREATE TABLE tags (
            tenant_id integer NOT NULL,
            name text NOT NULL,
            EXCLUDE (tenant_id WITH =, name WITH =)
        );
    """
    info = parse_schema_sql(sql)
    tags = info.table("tags")
    rows = data.draw(table_rows_strategy(tags, count=6))
    pairs = [(row["tenant_id"], row["name"]) for row in rows]
    assert len(set(pairs)) == len(pairs), f"Duplicate (tenant_id, name): {pairs}"


# ---------------------------------------------------------------------------
# Invariant (iii): non-equality exclusion doesn't crash the generator
# ---------------------------------------------------------------------------


def test_generator_tolerates_overlap_exclusion_without_enforcing() -> None:
    """Invariant (iii): an overlap-based exclusion (``WITH &&``)
    can't be enforced without range type support (#4b). The
    generator must produce rows without crashing; Postgres still
    rejects conflicts at INSERT time. Failure case: a
    schema-level KeyError or AttributeError trying to evaluate
    range overlap in the rejection sampler would break dataset
    generation for the entire schema."""
    sql = """
        CREATE TABLE bookings (
            id serial PRIMARY KEY,
            room integer NOT NULL,
            during tsrange NOT NULL,
            EXCLUDE USING gist (room WITH =, during WITH &&)
        );
    """
    info = parse_schema_sql(sql)
    bookings = info.table("bookings")

    @given(data=st.data())
    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def inner(data: st.DataObject) -> None:
        data.draw(table_rows_strategy(bookings, count=3))

    inner()
