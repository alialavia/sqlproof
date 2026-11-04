"""Existing scenarios load the same bytes and keep their fingerprints.

Recorded before value generators (`format`, `one_of`, `some_of`, `pick`),
`set` on fills and per-table growth existed, so those features cannot
change what an existing scenario loads: a scenario's data is what its
snapshots, scale baselines and cloud versions are keyed on. If one of
these fails, generated values or fingerprints changed: that is a
data-changing release, to be called out in the changelog before the
digest is updated.
"""

from __future__ import annotations

import hashlib
import itertools

import pytest

from sqlproof.scale.load import column_wrap_kinds, copy_line, generate_dataset
from sqlproof.scenarios import Scenario, scenario
from sqlproof.scenarios.serialize import dumps, scenario_from_dict
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TYPE plan_kind AS ENUM ('free', 'pro');
CREATE TABLE orgs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES auth.users(id)
);
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id),
  owner_id uuid NOT NULL REFERENCES auth.users(id),
  tier integer NOT NULL CHECK (tier BETWEEN 1 AND 3),
  plan plan_kind NOT NULL,
  email text NOT NULL UNIQUE,
  tags text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10,2) NOT NULL CHECK (total >= 0),
  lines jsonb,
  issued_on date NOT NULL DEFAULT current_date
);
"""


@pytest.fixture(scope="module")
def schema() -> SchemaInfo:
    return parse_schema_sql(SCHEMA)


@scenario(seed=7)
def everything(s):
    owner = s.row("auth.users", name="owner")
    s.table("auth.users").fill(2)
    acme = s.row("orgs", name="acme", slug="acme", settings={"theme": "dark"}, created_by=owner)
    s.table("orgs").cohort(2, settings={"theme": "light"}).fill(3)
    s.row("customers", name="big", org_id=acme, owner_id=owner, tier=1, plan="pro")
    s.table("customers").cohort(3, tier=2, tags=["vip", "eu"]).fill(6)
    s.table("invoices").per_parent(
        "customers", where={"tier": 2}, count=(2, 4), default=(0, 1)
    )


@scenario(seed=-3)
def per_named_parent(s):
    s.table("auth.users").fill(1)
    s.table("orgs").fill(2)
    big = s.row("customers", name="big", tier=3, plan="free")
    s.table("customers").fill(4)
    s.table("invoices").per_parent("customers", where=big, count=7, default=(1, 2))


def _digest(built: Scenario, schema: SchemaInfo) -> str:
    compiled = built.compile(schema)
    digest = hashlib.sha256()
    for table, rows in generate_dataset(
        schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns
    ):
        first = next(rows)
        names = list(first.keys())
        kinds = column_wrap_kinds(table, names)
        digest.update(table.qualified_name.encode())
        for row in itertools.chain([first], rows):
            digest.update(copy_line(row, names, kinds).encode())
    for user in compiled.auth_users:
        digest.update(f"{user.id}|{user.email}|{user.name}".encode())
    return digest.hexdigest()


def _variants() -> dict[str, Scenario]:
    yaml_roundtrip = scenario_from_dict(everything.to_dict())
    return {
        "everything": everything,
        "everything_scaled_4": everything.scaled(4),
        "everything_scaled_3_cohorts": everything.scaled(3, scale_cohorts=True),
        "everything_minimal": everything.minimal(),
        "everything_from_dict": yaml_roundtrip,
        "per_named_parent": per_named_parent,
        "per_named_parent_scaled_8": per_named_parent.scaled(8),
    }


GOLDEN: dict[str, tuple[str, str]] = {
    # name: (COPY bytes digest, scenario fingerprint)
    "everything": (
        "d6e02f0d78eaeb221592f81b1226f9221819d84d3e9d44a2ead0c44a8cc74c8e",
        "7907371d396eea55dbf27401713cb7e439be62b9a5cc6becc1eda8c4d77bf6d3",
    ),
    "everything_from_dict": (
        "d6e02f0d78eaeb221592f81b1226f9221819d84d3e9d44a2ead0c44a8cc74c8e",
        "7907371d396eea55dbf27401713cb7e439be62b9a5cc6becc1eda8c4d77bf6d3",
    ),
    "everything_minimal": (
        "38beebea8efe068191962cafbfd7f52ad3bfc440631681df48e2d2bcdf9fb599",
        "2aadc42b216981c24b5ca1b75d6e6933eba5ca83248e10db05386aed2d52a670",
    ),
    "everything_scaled_3_cohorts": (
        "388d0c61831d8e700f0f69d9d9d0de2a99187353584323e6f564176e04c7d169",
        "87b4c75e12b56942c82c1e00e0f5c5208a2b7e801a4eb0a8418bfb26c2025c5b",
    ),
    "everything_scaled_4": (
        "13dc0c00d1beb90f90d5589f3c4226ec6f8959bb53b4dc25232d24945ccf2d12",
        "0743bb69f1670788e1a424eb92e1ee313c9313d7fa1716eb894e91834abc15fb",
    ),
    "per_named_parent": (
        "d939dc36bbcec4b8bd2e8da67072c65aa138ef21e0441aff3bca294e1549e20f",
        "de739bde7edf5da29273278a028165a62d8969f9a21cb20244f381053c010c07",
    ),
    "per_named_parent_scaled_8": (
        "6ff6893d4ebe5b0712a229cb43400942a7413b7827c0ff258246f47ceabb48e2",
        "ea5c1a193c45e1c8ad5451f34019824bd48270b73bfd9ba68af2ac08d370d281",
    ),
}


@pytest.mark.parametrize("name", sorted(_variants()))
def test_existing_scenarios_load_the_same_bytes(name: str, schema: SchemaInfo) -> None:
    built = _variants()[name]
    assert (_digest(built, schema), built.fingerprint) == GOLDEN[name]


def test_the_serialized_form_of_an_existing_scenario_is_unchanged() -> None:
    assert hashlib.sha256(dumps(everything, "json").encode()).hexdigest() == SERIALIZED


SERIALIZED = "cdb517b8f26dd574ec5f59697a44dac0d5f9a0cc2d3b4ab88661aae784ded9f5"

