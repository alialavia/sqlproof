from __future__ import annotations

import json

import pytest

from sqlproof import SqlProof, sqlproof
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.runners import property as property_module


def test_sqlproof_decorator_injects_client_with_generated_data(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        """
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
        );
        """,
        encoding="utf-8",
    )
    proof = SqlProof.from_schema_file(schema_file)
    observed_sizes: list[int] = []

    @sqlproof(proof, sizes={"orders": 3}, runs=2)
    def property_holds(db) -> None:
        rows = db.query("SELECT total FROM orders")
        observed_sizes.append(len(rows))
        assert all(row["total"] >= 0 for row in rows)

    property_holds()

    assert observed_sizes == [3, 3]


def test_check_row_context_is_written_to_counterexample(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    failure_dir = tmp_path / "failures"
    schema_file.write_text(
        """
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          total INTEGER NOT NULL CHECK (total >= 0)
        );
        """,
        encoding="utf-8",
    )
    proof = SqlProof.from_schema_file(schema_file)

    @sqlproof(proof, sizes={"orders": 1}, runs=1, failure_dir=failure_dir)
    def property_fails(db, check) -> None:
        order = db.query("SELECT id, total FROM orders")[0]
        with check.row(order_id=order["id"]):
            raise AssertionError("boom")

    with pytest.raises(SqlProofPropertyFailure) as failure:
        property_fails()

    assert failure.value.counterexample is not None
    assert failure.value.counterexample["row_context"] == {"order_id": 1}
    payload = json.loads((failure_dir / "property_fails.json").read_text(encoding="utf-8"))
    assert payload["row_context"] == {"order_id": 1}


def test_runner_saves_hypothesis_shrunk_counterexample(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    failure_dir = tmp_path / "failures"
    schema_file.write_text(
        """
        CREATE TABLE users (
          id SERIAL PRIMARY KEY,
          name TEXT NOT NULL
        );
        """,
        encoding="utf-8",
    )
    proof = SqlProof.from_schema_file(schema_file)

    @sqlproof(proof, sizes={"users": 1}, runs=25, failure_dir=failure_dir)
    def property_fails_on_empty_name(db) -> None:
        name = db.scalar("SELECT name FROM users")
        if name == "":
            raise AssertionError("empty name")

    with pytest.raises(SqlProofPropertyFailure) as failure:
        property_fails_on_empty_name()

    assert failure.value.counterexample is not None
    assert failure.value.counterexample["dataset"]["users"][0]["name"] == ""
    payload = json.loads(
        (failure_dir / "property_fails_on_empty_name.json").read_text(encoding="utf-8")
    )
    assert payload["dataset"]["users"][0]["name"] == ""


def test_runner_uses_hypothesis_given_instead_of_manual_examples(tmp_path, monkeypatch) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text("CREATE TABLE users (id SERIAL PRIMARY KEY);", encoding="utf-8")
    proof = SqlProof.from_schema_file(schema_file)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("manual example draw should not be used")

    monkeypatch.setattr(property_module, "draw_example", fail_if_called, raising=False)

    @sqlproof(proof, sizes={"users": 1}, runs=2)
    def property_holds(db) -> None:
        assert db.scalar("SELECT id FROM users") == 1

    property_holds()


def test_invariant_fails_when_query_returns_rows(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        """
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          total INTEGER NOT NULL CHECK (total >= 0)
        );
        """,
        encoding="utf-8",
    )
    proof = SqlProof.from_schema_file(schema_file)

    with pytest.raises(SqlProofPropertyFailure):
        proof.invariant(
            "orders exist",
            sizes={"orders": 1},
            query="SELECT id FROM orders",
            runs=1,
        )
