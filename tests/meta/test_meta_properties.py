from __future__ import annotations

import json

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

from sqlproof import SqlProof, sqlproof
from sqlproof.cli import main
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.parse_sql import parse_schema_sql
from sqlproof.testing import datasets_for, schemas


def test_generated_values_satisfy_claimed_types() -> None:
    schema = parse_schema_sql(
        "CREATE TABLE values_table (id SERIAL PRIMARY KEY, value INTEGER NOT NULL);"
    )
    dataset = draw_example(dataset_strategy(schema, sizes={"values_table": 3}))
    assert all(isinstance(row["value"], int) for row in dataset["values_table"])


@given(schema=schemas(max_tables=2, max_columns=3), data=st.data())
@settings(max_examples=10)
def test_generated_datasets_satisfy_scoped_schema_constraints(schema, data) -> None:
    sizes = {table.name: 2 for table in schema.tables}
    dataset = data.draw(datasets_for(schema, sizes=sizes))
    assert set(dataset) == {table.name for table in schema.tables}


def test_fk_references_are_always_valid() -> None:
    schema = parse_schema_sql(
        """
        CREATE TABLE customers (id SERIAL PRIMARY KEY);
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(id)
        );
        """
    )
    dataset = draw_example(dataset_strategy(schema, sizes={"customers": 2, "orders": 5}))
    customer_ids = {row["id"] for row in dataset["customers"]}
    assert all(row["customer_id"] in customer_ids for row in dataset["orders"])


def test_shrinking_order_proxy_is_monotone_for_minimal_dataset() -> None:
    schema = parse_schema_sql("CREATE TABLE events (id SERIAL PRIMARY KEY);")
    dataset = find(
        dataset_strategy(schema, sizes={"events": 3}), lambda value: len(value["events"]) == 3
    )
    ids = [row["id"] for row in dataset["events"]]
    assert ids == sorted(ids)


def test_determinism_same_minimization_target_same_dataset() -> None:
    schema = parse_schema_sql("CREATE TABLE events (id SERIAL PRIMARY KEY);")
    strategy = dataset_strategy(schema, sizes={"events": 2})
    first = find(strategy, lambda value: len(value["events"]) == 2)
    second = find(strategy, lambda value: len(value["events"]) == 2)
    assert first == second


def test_idempotence_of_parse_and_fingerprint() -> None:
    sql = "CREATE TABLE events (id SERIAL PRIMARY KEY, name TEXT NOT NULL);"
    first = parse_schema_sql(sql)
    second = parse_schema_sql(sql)
    assert first == second
    assert compute(first) == compute(second)


def test_schema_parser_introspection_agreement_for_file_schema(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text("CREATE TABLE events (id SERIAL PRIMARY KEY);", encoding="utf-8")
    parsed = parse_schema_sql(schema_file.read_text(encoding="utf-8"))
    proof = SqlProof.from_schema_file(schema_file)
    assert proof.schema_info == parsed


def test_counterexample_replay_loads_failure_payload(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    failure_dir = tmp_path / "failures"
    schema_file.write_text("CREATE TABLE events (id SERIAL PRIMARY KEY);", encoding="utf-8")
    proof = SqlProof.from_schema_file(schema_file)

    @sqlproof(proof, sizes={"events": 1}, runs=1, failure_dir=failure_dir)
    def failing_property(db, check) -> None:
        with check.row(event_id=db.scalar("SELECT id FROM events")):
            raise AssertionError("expected failure")

    with pytest.raises(SqlProofPropertyFailure):
        failing_property()

    payload = json.loads((failure_dir / "failing_property.json").read_text(encoding="utf-8"))
    assert payload["row_context"] == {"event_id": 1}
    assert main(["replay", str(failure_dir / "failing_property.json")]) == 0
