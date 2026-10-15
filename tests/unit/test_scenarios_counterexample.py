"""Counterexamples as scenarios (scenarios phase 4).

The contract: the scenario pins every value the counterexample holds,
references stand in for keys so relationships survive the new keys, and
what the scenario loads is the counterexample's rows again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sqlproof.cli import main
from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import generate_dataset
from sqlproof.scenarios.counterexample import scenario_from_counterexample
from sqlproof.scenarios.serialize import dumps, load_file
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text);
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  owner_id uuid REFERENCES auth.users(id),
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  meta jsonb,
  tags text[],
  wait interval,
  stay int4range,
  embedding vector(3),
  full_name text GENERATED ALWAYS AS ('x') STORED
);
CREATE TABLE teams (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE memberships (
  customer_id bigint REFERENCES customers(id),
  team_id bigint REFERENCES teams(id),
  role text NOT NULL,
  PRIMARY KEY (customer_id, team_id)
);
CREATE TABLE invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  parent_id bigint REFERENCES invoices(id),
  total numeric(10, 2) NOT NULL
);
"""

ALICE = "6f1c1f8e-0000-4000-8000-000000000001"


@pytest.fixture
def schema() -> Any:
    return parse_schema_sql(SCHEMA_SQL)


def _payload(schema: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "property_name": "test_totals add up",
        "seed": 1234,
        "schema_fingerprint": compute(schema),
        "dataset": {
            "customers": [
                {
                    "id": 40,
                    "owner_id": ALICE,
                    "tier": 2,
                    "meta": {"plan": "pro", "seats": [1, 2]},
                    "tags": ["a", 'b "quoted"'],
                    "wait": 90.5,
                    "stay": {"__type__": "Range", "lower": 1, "upper": 5, "bounds": "[)"},
                    "embedding": [0.5, -1.0, 0.0],
                },
                {"id": 7, "owner_id": None, "tier": 1, "meta": None, "tags": []},
            ],
            "teams": [{"id": 3, "name": "red"}],
            "memberships": [{"customer_id": 7, "team_id": 3, "role": "owner"}],
            "invoices": [
                {"id": 100, "customer_id": 7, "parent_id": None, "total": "12.50"},
                {"id": 101, "customer_id": 40, "parent_id": 100, "total": "0.00"},
            ],
        },
    }


def test_rows_become_pinned_rows_and_keys_become_references(schema: Any) -> None:
    data = scenario_from_counterexample(_payload(schema), schema).to_dict()
    assert data["name"] == "counterexample_test_totals_add_up"
    assert data["seed"] == 1234
    tables = data["tables"]
    assert tables["auth.users"] == [{"row": "user_1"}]
    first, second = tables["customers"]
    assert first == {
        "row": "customers_1",
        "set": {
            "embedding": "[0.5,-1.0,0.0]",
            "meta": {"plan": "pro", "seats": [1, 2]},
            "owner_id": {"ref": "user_1"},
            "stay": '["1","5")',
            "tags": ["a", 'b "quoted"'],
            "tier": 2,
            "wait": "90.5 seconds",
        },
    }
    assert second["set"]["owner_id"] is None
    # A composite key is pinned through references; it has no single key to name.
    assert tables["memberships"] == [
        {
            "cohort": 1,
            "set": {
                "customer_id": {"ref": "customers_2"},
                "role": "owner",
                "team_id": {"ref": "teams_1"},
            },
        }
    ]
    # A self-reference points at the row's own table.
    assert tables["invoices"][1]["set"]["parent_id"] == {"ref": "invoices_1"}
    assert tables["invoices"][1]["set"]["customer_id"] == {"ref": "customers_1"}


def test_the_scenario_loads_the_counterexample_rows_again(schema: Any) -> None:
    payload = _payload(schema)
    scenario = scenario_from_counterexample(payload, schema)
    compiled = scenario.compile(schema)
    dataset = {
        table.name: list(rows)
        for table, rows in generate_dataset(
            schema, sizes=compiled.sizes, columns=compiled.columns, seed=0
        )
    }
    customers = dataset["customers"]
    assert [c["tier"] for c in customers] == [2, 1]
    assert customers[0]["owner_id"] == compiled.refs["user_1"]
    assert customers[0]["meta"] == {"plan": "pro", "seats": [1, 2]}
    key = {
        old["id"]: new["id"]
        for old, new in zip(payload["dataset"]["customers"], customers, strict=True)
    }
    [membership] = dataset["memberships"]
    assert membership["customer_id"] == key[7]
    assert membership["team_id"] == dataset["teams"][0]["id"]
    invoices = dataset["invoices"]
    assert [i["customer_id"] for i in invoices] == [key[7], key[40]]
    assert invoices[1]["parent_id"] == invoices[0]["id"]
    assert [i["total"] for i in invoices] == ["12.50", "0.00"]


def test_the_export_round_trips_through_yaml(schema: Any, tmp_path: Path) -> None:
    scenario = scenario_from_counterexample(_payload(schema), schema)
    path = tmp_path / "ce.scenario.yaml"
    path.write_text(dumps(scenario), encoding="utf-8")
    again = load_file(path)
    assert again.fingerprint == scenario.fingerprint


def test_a_counterexample_from_another_schema_is_refused(schema: Any) -> None:
    payload = _payload(schema)
    payload["dataset"]["orders"] = [{"id": 1}]
    with pytest.raises(SqlProofScenarioError, match="rows for 'orders', which the schema"):
        scenario_from_counterexample(payload, schema)
    with pytest.raises(SqlProofScenarioError, match="has no dataset"):
        scenario_from_counterexample({"property_name": "p"}, schema)


def test_cli_replay_as_scenario(
    schema: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    counterexample = tmp_path / "test_totals.json"
    counterexample.write_text(json.dumps(_payload(schema)), encoding="utf-8")

    assert main(["replay", str(counterexample)]) == 0
    assert "Reproduce the search with seed=1234." in capsys.readouterr().out

    out = tmp_path / "ce.scenario.json"
    code = main(
        [
            "replay",
            str(counterexample),
            "--as-scenario",
            "--schema-file",
            str(schema_file),
            "--name",
            "totals_failure",
            "--format",
            "json",
            "--output",
            str(out),
        ]
    )
    assert code == 0
    assert load_file(out).name == "totals_failure"

    with pytest.raises(SystemExit):
        main(["replay", str(counterexample), "--as-scenario"])
    assert "needs --schema-file or --dsn" in capsys.readouterr().err

    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"dataset": {"orders": [{"id": 1}]}}), encoding="utf-8")
    assert main(["replay", str(broken), "--as-scenario", "--schema-file", str(schema_file)]) == 2
    assert "rows for 'orders'" in capsys.readouterr().err


def test_a_changed_schema_is_a_warning(
    schema: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    payload = _payload(schema) | {"schema_fingerprint": "something else"}
    counterexample = tmp_path / "ce.json"
    counterexample.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        main(["replay", str(counterexample), "--as-scenario", "--schema-file", str(schema_file)])
        == 0
    )
    captured = capsys.readouterr()
    assert "the schema differs" in captured.err
    assert "name: counterexample_test_totals_add_up" in captured.out
