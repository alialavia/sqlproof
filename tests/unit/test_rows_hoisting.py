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
