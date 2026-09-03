from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from sqlproof.testing import schemas


@given(schemas(max_tables=3, max_columns=5))
@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
def test_generated_schemas_vary_in_shape(schema):
    assert 1 <= len(schema.tables) <= 3
    for table in schema.tables:
        assert 1 <= len(table.columns) <= 5
        assert table.primary_key


def test_schemas_eventually_produce_multiple_column_types():
    from hypothesis import find

    found = find(
        schemas(max_tables=1, max_columns=5),
        lambda s: len({c.type.name for c in s.tables[0].columns}) >= 2,
    )
    assert found is not None


def test_schemas_eventually_produce_a_nullable_column():
    from hypothesis import find

    found = find(
        schemas(max_tables=1, max_columns=5),
        lambda s: any(c.nullable for c in s.tables[0].columns),
    )
    assert found is not None


def test_schemas_eventually_produce_a_foreign_key():
    from hypothesis import find

    found = find(
        schemas(max_tables=3, max_columns=4),
        lambda s: any(t.foreign_keys for t in s.tables),
    )
    assert found is not None


def test_schemas_eventually_produce_a_check_constraint():
    from hypothesis import find

    found = find(
        schemas(max_tables=2, max_columns=4),
        lambda s: any(t.check_constraints for t in s.tables),
    )
    assert found is not None
