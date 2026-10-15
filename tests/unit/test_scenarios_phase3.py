"""Scenarios phase 3 without a database: scaling, calibration shape,
the Hypothesis strategy, and the sweep's argument checks."""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofScenarioError, SqlProofUsageError
from sqlproof.generators.typespec import DATETIME_WINDOW
from sqlproof.scale.args import argument_policy, heaviest
from sqlproof.scale.sweep import (  # pyright: ignore[reportPrivateUsage]
    ScenarioProfile,
    _with_refs,
    run_sweep,
)
from sqlproof.scenarios import DEFAULT_CLOCK, RowRef, ScenarioBuilder, scenario
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  name text NOT NULL,
  seen_at timestamp,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10, 2) NOT NULL CHECK (total >= 0)
);
"""


@pytest.fixture(scope="module")
def schema() -> SchemaInfo:
    return parse_schema_sql(SCHEMA_SQL)


@scenario(seed=5)
def shop(s: ScenarioBuilder) -> None:
    s.row("customers", name="acme", tier=1)
    s.table("customers").cohort(2, tier=1).cohort(3, tier=2).fill(10)
    s.table("invoices").per_parent("customers", where=s.ref("acme"), count=(4, 6), default=(0, 1))


def test_scaled_grows_fills_and_per_parent_only(schema: SchemaInfo) -> None:
    base = shop.compile(schema)
    four = shop.scaled(4).compile(schema)
    assert base.sizes["public.customers"] == 16
    assert four.sizes["public.customers"] == 1 + 2 + 3 + 40
    assert four.refs["acme"] == base.refs["acme"] == 1
    spec = shop.scaled(4).spec
    [invoices] = [t for t in spec.tables if t.name == "invoices"]
    assert invoices.per_parent is not None
    # acme is fixed, so its children grow; everyone else multiplies already.
    assert invoices.per_parent.count == (16, 24)
    assert invoices.per_parent.default == (0, 1)


def test_scaled_children_of_growing_parents_keep_their_counts() -> None:
    @scenario(seed=1)
    def every_parent(s: ScenarioBuilder) -> None:
        s.table("customers").fill(10)
        s.table("invoices").per_parent("customers", count=3)

    [_, invoices] = every_parent.scaled(5).spec.tables
    assert invoices.per_parent is not None and invoices.per_parent.count == (3, 3)

    @scenario(seed=1)
    def cohort_parents(s: ScenarioBuilder) -> None:
        s.table("customers").cohort(2, tier=1).fill(10)
        s.table("invoices").per_parent("customers", where={"tier": 1}, count=3)

    [_, fixed] = cohort_parents.scaled(5).spec.tables
    [_, grown] = cohort_parents.scaled(5, scale_cohorts=True).spec.tables
    assert fixed.per_parent is not None and fixed.per_parent.count == (15, 15)
    assert grown.per_parent is not None and grown.per_parent.count == (3, 3)


def test_scaled_totals_grow_linearly(schema: SchemaInfo) -> None:
    totals = [sum(shop.scaled(k).compile(schema).sizes.values()) for k in (1, 2, 4, 8)]
    ratios = [b / a for a, b in pairwise(totals)]
    assert all(1.5 < r < 2.2 for r in ratios), totals


def test_scaled_cohorts_grow_when_asked(schema: SchemaInfo) -> None:
    compiled = shop.scaled(3, scale_cohorts=True).compile(schema)
    assert compiled.sizes["public.customers"] == 1 + 6 + 9 + 30


def test_scaled_keeps_seed_name_and_clock() -> None:
    scaled = shop.scaled(2)
    assert (scaled.name, scaled.seed, scaled.clock) == ("shop", 5, DEFAULT_CLOCK)
    assert scaled.fingerprint != shop.fingerprint
    with pytest.raises(SqlProofScenarioError, match="scale factor"):
        shop.scaled(0)


def test_minimal_keeps_the_shape_with_little_data(schema: SchemaInfo) -> None:
    compiled = shop.minimal().compile(schema)
    # acme plus one row per cohort and one fill row.
    assert compiled.sizes["public.customers"] == 4
    assert compiled.refs == {"acme": 1}
    assert compiled.sizes["public.invoices"] <= 1


@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(data=st.data())
def test_strategy_draws_contain_every_pinned_row(data: st.DataObject) -> None:
    schema = parse_schema_sql(SCHEMA_SQL)
    dataset = data.draw(shop.strategy(schema))
    customers = dataset["customers"]
    assert len(customers) == 16
    assert customers[0]["tier"] == 1  # acme
    assert [c["tier"] for c in customers[1:3]] == [1, 1]
    assert [c["tier"] for c in customers[3:6]] == [2, 2, 2]
    # Volatile defaults are pinned to the scenario clock on this path too.
    assert {c["created_at"] for c in customers} == {DEFAULT_CLOCK}
    # Shared windows apply to unpinned values.
    for c in customers:
        assert c["seen_at"] is None or DATETIME_WINDOW[0] <= c["seen_at"] <= DATETIME_WINDOW[1]
    acme_invoices = [i for i in dataset["invoices"] if i["customer_id"] == 1]
    assert 4 <= len(acme_invoices) <= 6


def test_strategy_refuses_scenarios_that_seed_auth_users() -> None:
    schema = parse_schema_sql(
        "CREATE TABLE auth.users (id uuid PRIMARY KEY);"
        "CREATE TABLE notes (id bigint PRIMARY KEY, owner uuid REFERENCES auth.users(id));"
    )

    @scenario(seed=1)
    def with_users(s: ScenarioBuilder) -> None:
        s.table("auth.users").fill(2)
        s.table("notes").fill(2)

    with pytest.raises(SqlProofScenarioError, match=r"seeds auth\.users"):
        with_users.strategy(schema)


def test_ref_arguments_resolve_and_are_recorded() -> None:
    assert _with_refs([RowRef("acme"), 7], {"acme": 1}) == [1, 7]
    with pytest.raises(SqlProofUsageError, match=r"names no row of the scenario; named rows: acme"):
        _with_refs([RowRef("ghost")], {"acme": 1})
    policy = argument_policy([RowRef("acme"), heaviest("orgs.id"), 3])
    assert policy[0] == {"kind": "ref", "name": "acme"}
    assert policy[2] == {"kind": "literal"}


def test_scenario_profile_factor_zero_is_minimal(schema: SchemaInfo) -> None:
    profile = ScenarioProfile(shop, schema, scale_cohorts=False)
    assert profile(0).sizes == shop.minimal().compile(schema).sizes
    assert profile(2).sizes == shop.scaled(2).compile(schema).sizes
    assert profile(2).refs == {"acme": 1}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({}, "exactly one of sizes or scenario"),
        ({"sizes": {"customers": 1}, "scenario": shop}, "exactly one of sizes or scenario"),
        ({"scenario": shop, "columns": {}}, "columns cannot be combined"),
        ({"scenario": shop, "seed": 9}, "conflicts with scenario 'shop'"),
    ],
)
def test_run_sweep_argument_errors(
    schema: SchemaInfo, kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(SqlProofUsageError, match=match):
        run_sweep(None, schema, "f", **kwargs)  # type: ignore[arg-type]


def test_a_scenario_that_does_not_compile_is_refused_before_truncating(schema: SchemaInfo) -> None:
    @scenario(seed=1)
    def broken(s: ScenarioBuilder) -> None:
        s.table("customers").cohort(1, tier=9)

    with pytest.raises(SqlProofUsageError, match="refused before truncating anything"):
        run_sweep(None, schema, "f", scenario=broken)  # type: ignore[arg-type]
