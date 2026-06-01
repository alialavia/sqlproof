from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

import pytest
from hypothesis import find

from sqlproof import SqlProof, sqlproof
from sqlproof.cli import main
from sqlproof.coverage.diversity import diversity_ratio
from sqlproof.coverage.schema_shape import summarize_dataset_shape
from sqlproof.exceptions import SqlProofMappingError
from sqlproof.testing import schemas


class OrderRow(TypedDict):
    id: int
    total: int


@dataclass
class OrderModel:
    id: int
    total: int


def _proof(tmp_path) -> SqlProof:
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
    return SqlProof.from_schema_file(schema_file)


def test_query_typed_maps_typeddict_and_dataclass(tmp_path) -> None:
    proof = _proof(tmp_path)
    dataset = {"orders": [{"id": 1, "total": 2, "extra": "ignored"}]}
    with proof.client_for_dataset(dataset) as db:
        typed_rows = db.query_typed("SELECT id, total FROM orders", OrderRow)
        model_rows = db.query_typed("SELECT id, total FROM orders", OrderModel)

    assert typed_rows == [{"id": 1, "total": 2}]
    assert model_rows == [OrderModel(id=1, total=2)]


def test_query_typed_reports_missing_required_fields(tmp_path) -> None:
    proof = _proof(tmp_path)
    with (
        proof.client_for_dataset({"orders": [{"id": 1}]}) as db,
        pytest.raises(SqlProofMappingError),
    ):
        db.query_typed("SELECT id FROM orders", OrderModel)


def test_cli_version_and_introspect(tmp_path, capsys) -> None:
    proof = _proof(tmp_path)
    del proof
    assert main(["version"]) == 0
    assert main(["introspect", "--schema-file", str(tmp_path / "schema.sql")]) == 0
    output = capsys.readouterr().out
    assert "sqlproof" in output
    assert "orders" in output


def test_pytest_plugin_registers_database_url_flag(pytester) -> None:
    """The sqlproof pytest plugin registers exactly one CLI flag:
    ``--sqlproof-database-url``. The vestigial flags (--sqlproof-runs,
    --sqlproof-seed, etc.) were removed in #5 / PR #55 — they were
    declared but never wired into anything, so claiming them as a
    public surface was misleading. See pytest_plugin.py's
    pytest_addoption docstring for the rationale and the migration
    path for each removed flag.

    Failure case it addresses: silent regressions that re-introduce
    the noisy declared-but-no-op flags, or that drop the lone stable
    flag (--sqlproof-database-url) which IS wired and tested."""
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(["*--sqlproof-database-url=SQLPROOF_DATABASE_URL*"])


def test_capability_runner_decorators_execute(tmp_path) -> None:
    proof = _proof(tmp_path)
    calls: list[str] = []

    @sqlproof.stateful(proof, sizes={"orders": 1})
    class Lifecycle:
        def run(self, db) -> None:
            calls.append(str(db.scalar("SELECT id FROM orders")))

    @sqlproof.migration(
        proof,
        before_schema=str(tmp_path / "schema.sql"),
        migration=str(tmp_path / "schema.sql"),
        sizes={"orders": 1},
    )
    def migration_property(db_before, db_after) -> None:
        calls.append(str(db_before.scalar("SELECT id FROM orders")))
        calls.append(str(db_after.scalar("SELECT id FROM orders")))

    @sqlproof.rls(proof, sizes={"orders": 1}, roles=["authenticated"])
    def rls_property(db, user, all_data) -> None:
        calls.append(user["role"])
        calls.append(str(len(all_data["orders"])))

    @sqlproof.function_overloads(proof, function="calculate")
    def overload_property(db, call_a, call_b) -> None:
        calls.append(call_a.sql)
        calls.append(call_b.sql)

    Lifecycle().run()
    migration_property()
    rls_property()
    overload_property()

    assert calls


def test_coverage_helpers_and_testing_schema_strategy() -> None:
    dataset = {"orders": [{"id": 1, "total": 2}, {"id": 2, "total": 3}]}
    assert summarize_dataset_shape(dataset)["orders"]["rows"] == 2
    assert diversity_ratio([dataset, dataset]) == 0.5
    schema = find(schemas(max_tables=1, max_columns=2), lambda value: bool(value.tables))
    assert schema.tables
