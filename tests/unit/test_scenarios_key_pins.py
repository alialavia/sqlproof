"""Pinned primary keys, from the review of scenarios phase 4.

A key may be pinned only where it is made of references -- a join
table's composite key, or a single-column key that is also a foreign key
-- and then on every row, whole and distinct, so nothing the generator
assigns can collide with it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.generators.rows import PinnedKey
from sqlproof.scale.load import generate_dataset
from sqlproof.scenarios import RowRef, Scenario, ScenarioBuilder, scenario
from sqlproof.scenarios.counterexample import scenario_from_counterexample
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text);
CREATE TABLE profiles (id uuid PRIMARY KEY REFERENCES auth.users(id), handle text NOT NULL);
CREATE TABLE customers (id bigint PRIMARY KEY, tier int NOT NULL);
CREATE TABLE customer_settings (
  customer_id bigint PRIMARY KEY REFERENCES customers(id),
  theme text NOT NULL
);
CREATE TABLE notes (
  id bigint PRIMARY KEY,
  settings_id bigint NOT NULL REFERENCES customer_settings(customer_id)
);
CREATE TABLE teams (id bigint PRIMARY KEY);
CREATE TABLE memberships (
  customer_id bigint REFERENCES customers(id),
  team_id bigint REFERENCES teams(id),
  PRIMARY KEY (customer_id, team_id)
);
"""

SCHEMA = parse_schema_sql(SCHEMA_SQL)


def _rows(built: Scenario) -> dict[str, list[dict[str, Any]]]:
    compiled = built.compile(SCHEMA)
    return {
        table.name: list(rows)
        for table, rows in generate_dataset(
            SCHEMA, compiled.sizes, columns=compiled.columns, seed=built.seed
        )
    }


@scenario(seed=1)
def settings_for_two(s: ScenarioBuilder) -> None:
    s.row("customers", name="acme", tier=1)
    s.row("customers", name="initech", tier=2)
    s.table("customers").fill(3)
    s.row("customer_settings", name="acme_settings", customer_id=RowRef("initech"), theme="dark")
    s.row("customer_settings", name="other", customer_id=RowRef("acme"), theme="light")
    s.table("notes").cohort(2, settings_id=RowRef("acme_settings"))


def test_a_key_that_is_a_foreign_key_follows_its_reference() -> None:
    compiled = settings_for_two.compile(SCHEMA)
    assert isinstance(compiled.columns["public.customer_settings.customer_id"], PinnedKey)
    rows = _rows(settings_for_two)
    keys = [r["customer_id"] for r in rows["customer_settings"]]
    assert keys == [compiled.refs["initech"], compiled.refs["acme"]]
    # A named row's key is its parent's, and children reference it by name.
    assert compiled.refs["acme_settings"] == compiled.refs["initech"]
    assert {r["settings_id"] for r in rows["notes"]} == {compiled.refs["initech"]}


def test_the_hypothesis_path_honours_the_pinned_key_too() -> None:
    compiled = settings_for_two.compile(SCHEMA)
    seen: list[list[Any]] = []

    @settings(max_examples=5, deadline=None, suppress_health_check=list(HealthCheck))
    @given(dataset=settings_for_two.strategy(SCHEMA))
    def draws(dataset: dict[str, list[dict[str, Any]]]) -> None:
        seen.append([r["customer_id"] for r in dataset["customer_settings"]])

    draws()
    assert seen and all(k == [compiled.refs["initech"], compiled.refs["acme"]] for k in seen)


def test_a_plain_columns_override_on_a_key_is_still_ignored() -> None:
    rows = dict(
        (t.name, list(r))
        for t, r in generate_dataset(SCHEMA, {"customers": 3}, columns={"customers.id": 999})
    )
    assert [r["id"] for r in rows["customers"]] != [999, 999, 999]


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (
            lambda s: (
                s.table("customers").fill(2),
                s.table("customer_settings").cohort(2, customer_id=RowRef("a")),
            ),
            "must pin all of it, one row per group",
        ),
        (
            lambda s: (
                s.row("customers", name="a", tier=1),
                s.row("customer_settings", name="x", customer_id=RowRef("a")),
                s.table("customer_settings").fill(1),
            ),
            "must pin all of it",
        ),
        (
            lambda s: (
                s.row("customers", name="a", tier=1),
                s.table("customer_settings").cohort(1, customer_id=RowRef("a")),
                s.table("customer_settings").cohort(1, customer_id=RowRef("a")),
            ),
            "pin the same key",
        ),
        (
            lambda s: (
                s.table("customers").fill(2),
                s.table("customer_settings").cohort(1, customer_id=1),
            ),
            "pin it to a reference",
        ),
        (lambda s: s.table("customers").cohort(1, id=5), "part of the primary key"),
        (
            lambda s: (
                s.table("customers").fill(3),
                s.row("teams", name="red"),
                s.table("memberships").cohort(3, team_id=RowRef("red")),
            ),
            "memberships\\(customer_id, team_id\\) is pinned",
        ),
        (
            lambda s: (
                s.row("customers", name="a", tier=1),
                s.row("customer_settings", name="x", customer_id=RowRef("a")),
                s.table("notes").fill(2),
            ),
            "must pin settings_id to a named row",
        ),
    ],
)
def test_unsafe_key_pins_are_refused(build: Any, match: str) -> None:
    built = Scenario(build, seed=1, name="unsafe")
    with pytest.raises(SqlProofScenarioError, match=match):
        built.compile(SCHEMA)


def test_a_fully_pinned_composite_key_loads() -> None:
    @scenario(seed=2)
    def joined(s: ScenarioBuilder) -> None:
        s.row("customers", name="a", tier=1)
        s.row("customers", name="b", tier=1)
        s.row("teams", name="red")
        s.table("memberships").cohort(1, customer_id=RowRef("a"), team_id=RowRef("red"))
        s.table("memberships").cohort(1, customer_id=RowRef("b"), team_id=RowRef("red"))

    compiled = joined.compile(SCHEMA)
    pairs = [(r["customer_id"], r["team_id"]) for r in _rows(joined)["memberships"]]
    assert pairs == [
        (compiled.refs["a"], compiled.refs["red"]),
        (compiled.refs["b"], compiled.refs["red"]),
    ]


ALICE = "6f1c1f8e-0000-4000-8000-000000000001"


def test_counterexamples_keep_keys_that_are_foreign_keys() -> None:
    payload = {
        "property_name": "p",
        "schema_fingerprint": compute(SCHEMA),
        "dataset": {
            "users": [{"id": ALICE, "email": "a@x"}],
            "profiles": [{"id": ALICE, "handle": "alice"}],
            "customers": [{"id": 1, "tier": 1}, {"id": 2, "tier": 2}],
            "customer_settings": [{"customer_id": 2, "theme": "dark"}],
            "notes": [{"id": 10, "settings_id": 2}],
        },
    }
    built = scenario_from_counterexample(payload, SCHEMA)
    data = json.loads(json.dumps(built.to_dict()))
    assert data["tables"]["profiles"] == [
        {"row": "profiles_1", "set": {"handle": "alice", "id": {"ref": "user_1"}}}
    ]
    compiled = built.compile(SCHEMA)
    assert compiled.auth_users[0].name == "user_1"
    rows = _rows(built)
    [settings_row] = rows["customer_settings"]
    assert settings_row["customer_id"] == compiled.refs["customers_2"]
    assert rows["notes"][0]["settings_id"] == compiled.refs["customers_2"]
    assert rows["profiles"][0]["id"] == compiled.refs["user_1"]
