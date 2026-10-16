"""PostgreSQL is the independent oracle for scenario CHECK validation."""

import os
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import load_dataset
from sqlproof.scenarios import Scenario, ScenarioBuilder
from sqlproof.schema.parse_sql import parse_schema_sql

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)

NUMBERS = st.one_of(
    st.sampled_from([Decimal("1.00"), Decimal("2.00")]),
    st.decimals(-3, 3, places=2),
)
ARRAYS = st.lists(st.one_of(st.none(), st.integers(-10, 10)), max_size=4)


@pytest.mark.parametrize(
    ("sql_type", "check", "values"),
    [
        ("text", "value IN ('draft', 'paid')", st.sampled_from(["draft", "paid", "void", ""])),
        (
            "text",
            "value NOT IN ('void', 'blocked')",
            st.sampled_from(["draft", "paid", "void", "blocked", ""]),
        ),
        ("numeric", "value IN (1, 2)", NUMBERS),
        ("numeric", "value NOT IN (1, 2)", NUMBERS),
        ("integer[]", "cardinality(value) <= 2", ARRAYS),
        ("integer[]", "array_length(value, 1) >= 2", ARRAYS),
    ],
)
@settings(max_examples=25, deadline=None)
@given(data=st.data())
def test_scenario_check_validation_agrees_with_postgres(sql_type, check, values, data) -> None:
    """Pins are rejected exactly when PostgreSQL rejects their CHECK; accepted pins load intact."""
    value = data.draw(values)
    ddl = f"CREATE TEMPORARY TABLE items (id bigint PRIMARY KEY, value {sql_type} CHECK ({check}))"
    schema = parse_schema_sql(ddl, schema="pg_temp")

    def build(s: ScenarioBuilder) -> None:
        s.table("items").cohort(1, value=value)

    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute(ddl)
        (accepted,) = conn.execute(
            f"SELECT ({check}) IS NOT FALSE FROM (SELECT %s::{sql_type} AS value) AS candidate",
            (value,),
        ).fetchone()
        built = Scenario(build, seed=1, name="checked")
        if not accepted:
            with pytest.raises(SqlProofScenarioError, match="violates CHECK"):
                built.compile(schema)
        else:
            compiled = built.compile(schema)
            assert load_dataset(
                conn, schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns
            ) == {"items": 1}
            assert conn.execute("SELECT value FROM items").fetchall() == [(value,)]
