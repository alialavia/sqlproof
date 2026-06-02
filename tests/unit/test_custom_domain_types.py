"""Custom domain type support (#4, sub-feature 1).

Postgres lets users alias a base type with extra CHECK
constraints::

    CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
    CREATE TABLE products (qty positive_int NOT NULL);

A column declared as ``positive_int`` should generate as an
``integer`` filtered to positive values. Before this change the
parser fell back to a raw text type for unrecognized type names,
so ``strategy_for_type`` produced strings for ``qty`` and
Postgres rejected the INSERT with ``invalid input syntax for type
integer``.

Invariants pinned down here:

  (i) The parser captures ``CREATE DOMAIN`` statements with their
      base type and any CHECK expressions.
  (ii) A column whose declared type matches a domain is resolved
       to ``PgType(kind="domain", base=<the base type>)``.
  (iii) The row generator produces values for that column using
        the base type's strategy, AND filters them to satisfy the
        domain's CHECK constraints (``VALUE > 0`` translated to
        the column's name).

Failure cases each invariant addresses:
  (i): Without parsing the CREATE DOMAIN, ``positive_int`` is just
       a string token to the rest of the parser; downstream code
       has no way to know its base type or constraints.
  (ii): Without resolving the column's type to the domain, the
        generator falls back to text — every numeric domain
        becomes random text, INSERTs blow up immediately.
  (iii): Even with type resolution, ignoring the CHECK means the
         generator emits ``0`` or negative integers, which fail
         the domain constraint and get rejected at INSERT time.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql

# ---------------------------------------------------------------------------
# Invariant (i) + (ii): parser captures the domain, resolves the column type
# ---------------------------------------------------------------------------


def test_parser_resolves_column_type_to_domain_with_base() -> None:
    """Invariants (i) + (ii): a column typed with a custom domain
    surfaces as kind=domain with the right base. Failure case: an
    unrecognized type name silently became a scalar text type."""
    schema = parse_schema_sql(
        """
        CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
        CREATE TABLE products (
            id serial PRIMARY KEY,
            qty positive_int NOT NULL
        );
        """
    )
    qty = schema.table("products").column("qty")
    assert qty.type.kind == "domain"
    assert qty.type.name == "positive_int"
    assert qty.type.base is not None
    assert qty.type.base.name == "integer"


# ---------------------------------------------------------------------------
# Invariant (iii): generator applies the domain's CHECK constraints
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_respects_domain_check_constraint(data: st.DataObject) -> None:
    """Invariant (iii): values emitted for a domain column satisfy
    the domain's CHECK expression. Failure case: random integer
    values include 0 and negatives, both of which violate the
    canonical ``VALUE > 0`` domain constraint."""
    schema = parse_schema_sql(
        """
        CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
        CREATE TABLE products (
            id serial PRIMARY KEY,
            qty positive_int NOT NULL
        );
        """
    )
    products = schema.table("products")
    rows = data.draw(table_rows_strategy(products, count=5))
    for row in rows:
        assert isinstance(row["qty"], int), (
            f"qty must be an int (base type), got {type(row['qty']).__name__}"
        )
        assert row["qty"] > 0, f"Domain constraint VALUE > 0 violated: {row['qty']}"


# ---------------------------------------------------------------------------
# Regression: domains without CHECKs still resolve correctly
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_domain_without_check_generates_base_type_values(
    data: st.DataObject,
) -> None:
    """Regression: a ``CREATE DOMAIN`` with no CHECK should just
    alias the base type. Failure case: a refactor of the CHECK
    inheritance path that accidentally requires CHECKs to be
    present would break domains-as-pure-aliases (e.g. email-as-text
    aliases without validation)."""
    schema = parse_schema_sql(
        """
        CREATE DOMAIN email_address AS text;
        CREATE TABLE accounts (
            id serial PRIMARY KEY,
            email email_address NOT NULL
        );
        """
    )
    accounts = schema.table("accounts")
    rows = data.draw(table_rows_strategy(accounts, count=3))
    for row in rows:
        assert isinstance(row["email"], str), (
            f"email must resolve to text, got {type(row['email']).__name__}"
        )
