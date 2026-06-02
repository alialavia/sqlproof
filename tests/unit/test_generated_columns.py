"""Generated column support (#3, sub-feature 2).

Two flavors of generated columns exist in Postgres:

  (a) ``IDENTITY`` columns — handled today via the ``identity`` /
      ``is_generated`` paths set up alongside ``SERIAL``.
  (b) ``GENERATED ALWAYS AS (expr) STORED`` columns — values
      computed from other columns on each row. These do NOT appear
      in INSERTs (Postgres rejects an INSERT that targets a
      generated column).

Before this change, the parser only flagged ``is_generated=True``
when the column was ``SERIAL``/``BIGSERIAL`` or had an
``IDENTITY`` clause. ``GENERATED ALWAYS AS (qty * unit_price)
STORED``-style columns were treated as regular columns, the row
generator drew random values for them, and Postgres rejected the
INSERT with ``cannot insert a non-DEFAULT value into column``.

The introspector path was already correct because it reads
``pg_attribute.attgenerated`` directly (line ``att.attgenerated <>
''`` evaluates True for stored generated columns), so this fix is
purely on the parser side.

Invariants pinned down here:

  (i) The SQL parser flags ``GENERATED ALWAYS AS (...) STORED``
      columns as ``is_generated=True``.
  (ii) The row generator does NOT emit a value for such columns
       (they're skipped entirely; Postgres computes them).
  (iii) Existing identity/serial handling is unaffected (regression
        guard).

Failure cases each invariant addresses:
  (i): Generated-column schemas silently lost the ``is_generated``
       flag at parse time; downstream generation tried to populate
       the column and Postgres rejected the INSERT with a hard
       error that didn't point at the schema mismatch.
  (ii): Even if the flag were set, the row generator must actually
        SKIP the column (not just emit None) — Postgres rejects
        explicit NULL into a generated column the same as it
        rejects any other value.
  (iii): IDENTITY / SERIAL columns must still be detected; the
         parser shouldn't drop those paths in pursuit of the new
         one.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE line_items (
    id serial PRIMARY KEY,
    qty integer NOT NULL,
    unit_price numeric(10, 2) NOT NULL,
    amount_total numeric(10, 2) GENERATED ALWAYS AS (qty * unit_price) STORED
);
"""


# ---------------------------------------------------------------------------
# Invariant (i): parser detects GENERATED ALWAYS AS columns
# ---------------------------------------------------------------------------


def test_parser_flags_stored_generated_column_as_is_generated() -> None:
    """Invariant (i): a ``GENERATED ALWAYS AS (...) STORED`` column
    must surface with ``is_generated=True``. Failure case: the
    parser previously only flagged SERIAL / IDENTITY; stored
    generated columns slipped through and the generator tried to
    populate them."""
    schema = parse_schema_sql(SCHEMA_SQL)
    line_items = schema.table("line_items")
    amount_total = line_items.column("amount_total")
    assert amount_total.is_generated is True


def test_parser_still_flags_serial_columns_as_is_generated() -> None:
    """Invariant (iii, regression): the existing SERIAL detection
    must keep working — the new branch should be additive, not a
    replacement. Failure case: a refactor that drops the SERIAL
    check would re-introduce a different version of this bug for
    SERIAL columns."""
    schema = parse_schema_sql(SCHEMA_SQL)
    line_items = schema.table("line_items")
    assert line_items.column("id").is_generated is True


def test_parser_does_not_flag_regular_columns() -> None:
    """Regression: only generated/identity/serial columns should
    get the flag. Failure case: an overly-broad check would set
    is_generated on every NOT NULL column, breaking generation for
    schemas with required user-supplied columns."""
    schema = parse_schema_sql(SCHEMA_SQL)
    line_items = schema.table("line_items")
    assert line_items.column("qty").is_generated is False
    assert line_items.column("unit_price").is_generated is False


# ---------------------------------------------------------------------------
# Invariant (ii): row generator skips generated columns
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_row_generator_does_not_emit_generated_column(
    data: st.DataObject,
) -> None:
    """Invariant (ii): rows emitted by the generator must not have
    a key for the ``amount_total`` column — Postgres rejects an
    INSERT that targets a stored generated column, whether the
    value is real, NULL, or DEFAULT. Failure case: pre-fix the
    parser missed the flag, so the generator drew random numerics
    for ``amount_total`` and Postgres slammed the door."""
    schema = parse_schema_sql(SCHEMA_SQL)
    table = schema.table("line_items")
    rows = data.draw(table_rows_strategy(table, count=3))
    for row in rows:
        assert "amount_total" not in row, (
            f"Generator emitted value for stored generated column: {row}"
        )
