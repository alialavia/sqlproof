"""Composite type support (#4, sub-feature 3).

Postgres lets users define record-shaped types::

    CREATE TYPE addr AS (street text, zip text);
    CREATE TABLE shipments (
        id serial PRIMARY KEY,
        destination addr NOT NULL
    );

Before this change, ``addr`` slipped through the parser as an
unrecognized scalar, the row generator emitted random text, and
Postgres rejected the INSERT with ``invalid input syntax for type
addr``.

After this change, columns typed with a defined composite type
resolve to ``PgType(kind="composite", composite_fields=...)`` and
the generator emits a dict ``{field_name: field_value}`` shaped
to match the composite definition recursively (composite-of-
composite, e.g. ``CREATE TYPE customer AS (name text, address
addr)``, also works).

Invariants pinned down here:

  (i) The parser surfaces ``CREATE TYPE foo AS (...)`` definitions
      with their field name + type pairs, and resolves columns
      that reference the composite to ``kind="composite"`` with
      the field list attached.
  (ii) Generated values for a composite column are dicts whose
       keys match the composite's field names and whose values
       are of the right element type.
  (iii) Composites can nest: a composite field can itself be
        another composite, and the generator walks the structure
        recursively.

Failure cases each invariant addresses:
  (i): Without parser support, ``addr`` is a string token; the
       generator has no signal that it should emit structured
       values.
  (ii): A flat text fallback can't produce values Postgres will
        accept for a composite column — psycopg's adapter needs
        either a registered composite class or a properly-shaped
        tuple/dict.
  (iii): A non-recursive resolution would treat the nested
         composite as scalar text again, regressing at the
         second level.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql

# ---------------------------------------------------------------------------
# Invariant (i): parser resolves composite type definitions
# ---------------------------------------------------------------------------


def test_parser_captures_composite_type_definition() -> None:
    """Invariant (i): a CREATE TYPE ... AS (...) statement is
    captured with its field name + type pairs. Failure case: the
    parser previously walked only CreateStmt/CreateEnumStmt, so
    CompositeTypeStmt nodes fell off entirely."""
    schema = parse_schema_sql(
        """
        CREATE TYPE addr AS (street text, zip text);
        CREATE TABLE shipments (
            id serial PRIMARY KEY,
            destination addr NOT NULL
        );
        """
    )
    destination = schema.table("shipments").column("destination")
    assert destination.type.kind == "composite"
    assert destination.type.name == "addr"
    field_names = [name for name, _ in destination.type.composite_fields]
    assert field_names == ["street", "zip"]
    field_kinds = [field_type.name for _, field_type in destination.type.composite_fields]
    assert field_kinds == ["text", "text"]


# ---------------------------------------------------------------------------
# Invariant (ii): generator emits dict matching the field shape
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_produces_dict_matching_composite_fields(
    data: st.DataObject,
) -> None:
    """Invariant (ii): the generated value is a dict whose keys
    exactly match the composite's field names. Failure case: a
    flat text fallback or a tuple with wrong arity would fail
    Postgres's type check at INSERT time."""
    schema = parse_schema_sql(
        """
        CREATE TYPE addr AS (street text, zip text);
        CREATE TABLE shipments (
            id serial PRIMARY KEY,
            destination addr NOT NULL
        );
        """
    )
    shipments = schema.table("shipments")
    rows = data.draw(table_rows_strategy(shipments, count=3))
    for row in rows:
        dest = row["destination"]
        assert isinstance(dest, dict), (
            f"Composite column must produce dict, got {type(dest).__name__}"
        )
        assert set(dest.keys()) == {"street", "zip"}
        assert isinstance(dest["street"], str)
        assert isinstance(dest["zip"], str)


# ---------------------------------------------------------------------------
# Invariant (iii): composite types nest recursively
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_walks_nested_composite_types_recursively(
    data: st.DataObject,
) -> None:
    """Invariant (iii): a composite type with a composite field
    generates the right nested structure. Failure case: shallow
    resolution would treat the nested ``home`` field as scalar
    text, regressing at the second level."""
    schema = parse_schema_sql(
        """
        CREATE TYPE addr AS (street text, zip text);
        CREATE TYPE customer AS (name text, home addr);
        CREATE TABLE accounts (
            id serial PRIMARY KEY,
            owner customer NOT NULL
        );
        """
    )
    accounts = schema.table("accounts")
    rows = data.draw(table_rows_strategy(accounts, count=3))
    for row in rows:
        owner = row["owner"]
        assert isinstance(owner, dict)
        assert set(owner.keys()) == {"name", "home"}
        assert isinstance(owner["name"], str)
        assert isinstance(owner["home"], dict)
        assert set(owner["home"].keys()) == {"street", "zip"}
