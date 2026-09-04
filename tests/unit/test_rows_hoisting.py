from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from sqlproof.generators import rows as rows_module
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql


def test_column_strategy_built_once_per_column_not_once_per_row(monkeypatch):
    schema = parse_schema_sql(
        "CREATE TABLE t (id bigint PRIMARY KEY, a text NOT NULL, b text NOT NULL);"
    )
    table = schema.table("t")
    calls = {"n": 0}
    original = rows_module.strategy_for_column

    def counting(column):
        calls["n"] += 1
        return original(column)

    monkeypatch.setattr(rows_module, "strategy_for_column", counting)

    @given(table_rows_strategy(table, count=50))
    @settings(
        max_examples=1,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    def run(generated):
        assert len(generated) == 50

    run()
    # 2 non-PK columns. Without hoisting this is 100 (50 rows x 2 columns).
    assert calls["n"] <= 2


def test_column_override_shields_undimensioned_vector_column_from_hoist():
    """An overridden column must never reach strategy_for_column, hoisted
    or not.

    `vector` with no dimension is schema-valid SQL (parse_schema_sql
    accepts it) but strategy_for_column raises SqlProofSchemaError for
    it (a dimension is required to draw values). A `columns=` override
    is exactly the caller's escape hatch for a column like this: the
    per-row loop's override branch short-circuits before it ever
    reaches strategy_for_column, so the hoist loop must skip such
    columns too instead of eagerly building (and raising on) a
    strategy that will never be used.
    """
    schema = parse_schema_sql(
        "CREATE TABLE t (id bigint PRIMARY KEY, embedding vector NOT NULL);"
    )
    table = schema.table("t")

    # Must not raise during strategy construction...
    strategy = table_rows_strategy(
        table, count=2, columns={"t.embedding": [0.1, 0.2, 0.3]}
    )

    @given(strategy)
    @settings(
        max_examples=1,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    def run(generated):
        # ...and rows must actually be generated, using the override.
        assert len(generated) == 2
        for row in generated:
            assert row["embedding"] == [0.1, 0.2, 0.3]

    run()
