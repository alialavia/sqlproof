from __future__ import annotations

import json
from typing import Any, cast

import pytest
from hypothesis import find
from hypothesis import strategies as st
from psycopg.types.json import Jsonb

from sqlproof import ExternalTableSpec, SqlProof, sqlproof
from sqlproof.core import _external_parent_rows, _insert_dataset
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.runners import property as property_module
from sqlproof.schema.model import Column, PgType, SchemaInfo, Table


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


def test_sqlproof_exposes_dataset_strategy_with_shrinkable_sizes(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
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

    minimal_dataset = find(
        proof.dataset_strategy(sizes={"users": st.integers(min_value=1, max_value=3)}),
        lambda dataset: len(dataset["users"]) >= 2,
    )

    assert len(minimal_dataset["users"]) == 2


def test_sqlproof_dataset_strategy_accepts_column_overrides(tmp_path) -> None:
    schema_file = tmp_path / "schema.sql"
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

    dataset = find(
        proof.dataset_strategy(sizes={"users": 1}, columns={"users.name": "Ripenn"}),
        lambda _: True,
    )

    assert dataset["users"] == [{"id": 1, "name": "Ripenn"}]


def test_external_table_specs_seed_and_sample_parent_rows() -> None:
    calls: list[str] = []

    class FakeClient:
        def execute(self, sql: str, *params: object) -> int:
            del sql, params
            calls.append("seed")
            return 1

    rows = _external_parent_rows(
        {
            "auth.users": ExternalTableSpec(
                primary_key="id",
                seed=lambda db: db.execute("INSERT INTO auth.users DEFAULT VALUES"),
                sample=lambda db: ["user-1", "user-2"],
            )
        },
        cast(Any, FakeClient()),
    )

    assert calls == ["seed"]
    assert rows["auth.users"] == [{"id": "user-1"}, {"id": "user-2"}]
    assert rows["users"] == rows["auth.users"]


def test_external_table_seed_count_can_shrink() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.ids: list[int] = []

    client = FakeClient()

    def seed(db: FakeClient, count: int) -> None:
        db.ids = list(range(count))

    def sample(db: FakeClient) -> list[int]:
        return db.ids

    def generated_external_rows():
        @st.composite
        def rows(draw):
            return _external_parent_rows(
                {
                    "auth.users": ExternalTableSpec(
                        primary_key="id",
                        seed=seed,
                        seed_count=st.integers(min_value=1, max_value=5),
                        sample=sample,
                    )
                },
                cast(Any, client),
                draw=draw,
            )

        return rows()

    minimal_rows = find(
        generated_external_rows(),
        lambda rows: len(rows["auth.users"]) >= 3,
    )

    assert minimal_rows["auth.users"] == [{"id": 0}, {"id": 1}, {"id": 2}]


def test_external_table_seed_count_limits_sampled_parent_rows() -> None:
    class FakeClient:
        pass

    rows = _external_parent_rows(
        {
            "auth.users": ExternalTableSpec(
                primary_key="id",
                seed=lambda db, count: None,
                seed_count=2,
                sample=lambda db: ["user-1", "user-2", "user-3"],
            )
        },
        cast(Any, FakeClient()),
    )

    assert rows["auth.users"] == [{"id": "user-1"}, {"id": "user-2"}]


def test_external_table_sampling_uses_cache_when_no_seed() -> None:
    calls = 0

    class FakeClient:
        pass

    def sample(db: FakeClient) -> list[str]:
        nonlocal calls
        calls += 1
        return ["user-1", "user-2", "user-3"]

    cache: dict[str, list[object]] = {}
    spec = ExternalTableSpec(
        primary_key="id",
        seed_count=2,
        sample=sample,
    )

    first_rows = _external_parent_rows(
        {"auth.users": spec},
        cast(Any, FakeClient()),
        sample_cache=cache,
    )
    second_rows = _external_parent_rows(
        {"auth.users": spec},
        cast(Any, FakeClient()),
        sample_cache=cache,
    )

    assert calls == 1
    assert first_rows["auth.users"] == [{"id": "user-1"}, {"id": "user-2"}]
    assert second_rows["auth.users"] == first_rows["auth.users"]


def test_insert_dataset_adapts_jsonb_values() -> None:
    captured_params: tuple[object, ...] = ()
    schema_info = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="brand_config",
                columns=(
                    Column(
                        name="id",
                        type=PgType(kind="scalar", name="uuid"),
                        nullable=False,
                        default=None,
                        is_generated=False,
                    ),
                    Column(
                        name="strategy_weights",
                        type=PgType(kind="scalar", name="jsonb"),
                        nullable=False,
                        default=None,
                        is_generated=False,
                    ),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    class FakeClient:
        def execute(self, sql: str, *params: object) -> int:
            nonlocal captured_params
            del sql
            captured_params = params
            return 1

    _insert_dataset(
        cast(Any, FakeClient()),
        schema_info,
        {
            "brand_config": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "strategy_weights": {"organic": 1},
                }
            ]
        },
    )

    assert isinstance(captured_params[1], Jsonb)
    assert captured_params[1].obj == {"organic": 1}


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
