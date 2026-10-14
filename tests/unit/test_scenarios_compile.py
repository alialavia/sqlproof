"""Scenario compilation, without a database.

A scenario compiles to the bulk loader's existing inputs -- `sizes` and
row-index overrides -- so these tests compile against parsed schemas and
run the same generator `load_dataset` runs (`generate_dataset`), checking
the rows a load would send.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime
from typing import Any

import pytest

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import column_wrap_kinds, copy_line, generate_dataset
from sqlproof.scenarios import RowRef, Scenario, ScenarioBuilder, scenario
from sqlproof.scenarios.compile import CompiledScenario
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TYPE plan_kind AS ENUM ('free', 'pro');
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES auth.users(id),
  tier integer NOT NULL CHECK (tier BETWEEN 1 AND 3),
  plan plan_kind NOT NULL,
  email text NOT NULL UNIQUE,
  nickname text CHECK (char_length(nickname) <= 8),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id bigint NOT NULL REFERENCES customers(id),
  external_ref uuid NOT NULL DEFAULT gen_random_uuid(),
  total numeric(10,2) NOT NULL CHECK (total >= 0),
  issued_on date NOT NULL DEFAULT current_date,
  note text DEFAULT clock_timestamp()::text
);
"""


@pytest.fixture(scope="module")
def schema() -> SchemaInfo:
    return parse_schema_sql(SCHEMA)


def _rows(compiled: CompiledScenario, schema: SchemaInfo) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for table, rows in generate_dataset(
        schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns
    ):
        out[table.name] = list(rows)
    return out


@scenario(seed=42)
def tiered(s):
    alice = s.row("auth.users", name="alice")
    s.table("auth.users").fill(3)
    s.row("customers", name="acme", tier=1, owner_id=alice, plan="pro")
    s.table("customers").cohort(1, tier=1).cohort(3, tier=2).fill(5)
    s.table("invoices").per_parent("customers", where={"tier": 1}, count=(5, 10), default=(0, 2))


def test_cohorts_pin_their_rows_and_the_generator_fills_the_rest(schema: SchemaInfo) -> None:
    rows = _rows(tiered.compile(schema), schema)["customers"]
    assert len(rows) == 10
    assert [r["tier"] for r in rows[:5]] == [1, 1, 2, 2, 2]
    assert all(r["tier"] in {1, 2, 3} for r in rows[5:])
    assert rows[0]["plan"] == "pro"
    assert all(r["plan"] in {"free", "pro"} for r in rows)


def test_named_rows_resolve_to_their_keys(schema: SchemaInfo) -> None:
    compiled = tiered.compile(schema)
    rows = _rows(compiled, schema)["customers"]
    assert compiled.refs["acme"] == rows[0]["id"] == 1
    assert rows[0]["owner_id"] == compiled.refs["alice"]
    assert compiled.refs["alice"] == compiled.auth_users[0].id


def test_auth_users_are_seeded_and_referenced(schema: SchemaInfo) -> None:
    compiled = tiered.compile(schema)
    ids = {user.id for user in compiled.auth_users}
    assert len(compiled.auth_users) == 4
    assert [u.name for u in compiled.auth_users] == ["alice", None, None, None]
    assert len({u.email for u in compiled.auth_users}) == 4
    customers = _rows(compiled, schema)["customers"]
    assert {r["owner_id"] for r in customers} <= ids
    assert "public.users" not in compiled.sizes and "auth.users" not in compiled.sizes


def test_per_parent_gives_each_matching_parent_its_count(schema: SchemaInfo) -> None:
    compiled = tiered.compile(schema)
    rows = _rows(compiled, schema)
    tier_of = {c["id"]: c["tier"] for c in rows["customers"]}
    per_customer = {cid: 0 for cid in tier_of}
    for invoice in rows["invoices"]:
        per_customer[invoice["customer_id"]] += 1
    for cid, count in per_customer.items():
        # Only the two pinned tier-1 rows match `where`; generated tier-1
        # rows in the fill are not pinned, so they get the default.
        if cid in (1, 2):
            assert 5 <= count <= 10
        else:
            assert 0 <= count <= 2
    assert compiled.sizes["public.invoices"] == len(rows["invoices"])


def test_volatile_defaults_become_deterministic(schema: SchemaInfo) -> None:
    compiled = tiered.compile(schema)
    rows = _rows(compiled, schema)
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    assert all(r["created_at"] == clock for r in rows["customers"])
    assert all(r["issued_on"] == clock.date() for r in rows["invoices"])
    refs = [r["external_ref"] for r in rows["invoices"]]
    assert len(set(refs)) == len(refs)
    assert compiled.nondeterministic_defaults == ("public.invoices.note: clock_timestamp()::text",)


def test_a_custom_clock_moves_every_timestamp(schema: SchemaInfo) -> None:
    clock = datetime(2030, 6, 1, 12, tzinfo=UTC)
    moved = Scenario(tiered._build, seed=42, name="tiered", clock=clock)  # type: ignore[attr-defined]
    rows = _rows(moved.compile(schema), schema)
    assert {r["created_at"] for r in rows["customers"]} == {clock}


def test_same_scenario_same_rows(schema: SchemaInfo) -> None:
    assert _rows(tiered.compile(schema), schema) == _rows(tiered.compile(schema), schema)


def test_a_different_seed_changes_the_rows(schema: SchemaInfo) -> None:
    other = Scenario(tiered._build, seed=43, name="tiered")  # type: ignore[attr-defined]
    assert _rows(other.compile(schema), schema) != _rows(tiered.compile(schema), schema)


def _copy_digest(compiled: CompiledScenario, schema: SchemaInfo) -> str:
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
    return digest.hexdigest()


GOLDEN_DIGEST = "ab7b626a1a600dab5dff5f710360ca5e6bd6312b4806d9752c6f64926e18ea88"


def test_golden_copy_bytes(schema: SchemaInfo) -> None:
    """The exact bytes a load sends. If this fails, generated values
    changed: that is a data-changing release and must be called out in the
    changelog, then this digest updated."""
    assert _copy_digest(tiered.compile(schema), schema) == GOLDEN_DIGEST


def test_fingerprint_is_stable_and_tracks_the_spec() -> None:
    assert tiered.fingerprint == tiered.fingerprint
    other = Scenario(tiered._build, seed=43, name="tiered")  # type: ignore[attr-defined]
    assert other.fingerprint != tiered.fingerprint


def test_to_dict_is_the_serializable_form() -> None:
    data = tiered.to_dict()
    assert data["name"] == "tiered"
    assert data["seed"] == 42
    assert data["tables"]["customers"][0] == {
        "row": "acme",
        "set": {"owner_id": {"ref": "alice"}, "plan": "pro", "tier": 1},
    }
    assert data["tables"]["invoices"][0]["per_parent"]["count"] == [5, 10]


def test_to_dict_refuses_callables_but_fingerprint_accepts_them() -> None:
    @scenario(seed=1)
    def with_callable(s):
        s.table("customers").cohort(1, nickname=lambda ctx: f"n{ctx.row_index}")

    assert with_callable.fingerprint
    with pytest.raises(SqlProofScenarioError, match="callable"):
        with_callable.to_dict()


def test_callable_pins_receive_the_row_context(schema: SchemaInfo) -> None:
    @scenario(seed=1)
    def nicknames(s):
        s.table("auth.users").fill(1)
        s.table("customers").cohort(3, nickname=lambda ctx: f"n{ctx.row_index}")

    rows = _rows(nicknames.compile(schema), schema)["customers"]
    assert [r["nickname"] for r in rows] == ["n0", "n1", "n2"]


def test_qualified_and_bare_names_are_the_same_table(schema: SchemaInfo) -> None:
    @scenario(seed=1)
    def both(s):
        s.table("auth.users").fill(1)
        s.table("customers").fill(1)
        s.table("public.customers").fill(1)

    with pytest.raises(SqlProofScenarioError, match="declared twice"):
        both.compile(schema)


def _compile_error(schema: SchemaInfo, build, match: str) -> None:
    with pytest.raises(SqlProofScenarioError, match=match):
        Scenario(build, seed=1, name="bad").compile(schema)


def _users(s: ScenarioBuilder) -> None:
    """Satisfy customers.owner_id so a later validation is what fails."""
    s.table("auth.users").fill(1)


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda s: s.table("nope").fill(1), "unknown table 'nope'"),
        (lambda s: s.table("users").fill(1), "use 'auth.users'"),
        (lambda s: s.table("customers").cohort(1, colour="red"), "has no column 'colour'"),
        (lambda s: s.table("customers").cohort(1, id=5), "part of the primary key"),
        (lambda s: (_users(s), s.table("customers").cohort(1, tier=7)), "violates CHECK"),
        (
            lambda s: (_users(s), s.table("customers").cohort(1, nickname="far-too-long")),
            "violates CHECK",
        ),
        (lambda s: (_users(s), s.table("customers").cohort(1, plan="gold")), "not a value of"),
        (lambda s: (_users(s), s.table("customers").cohort(1, tier=None)), "NOT NULL"),
        (lambda s: (_users(s), s.table("customers").cohort(2, email="dup@x")), "pinned on 2 rows"),
        (
            lambda s: (_users(s), s.table("customers").fill(3).cohort(1, email="email_1")),
            "generator assigns email_1",
        ),
        (
            lambda s: s.table("customers").cohort(1, owner_id=RowRef("ghost")),
            "row 'ghost', which is not declared",
        ),
        (
            lambda s: (
                s.table("auth.users").fill(1),
                s.row("customers", name="c1"),
                s.table("invoices").cohort(1, customer_id=RowRef("c1")),
                s.table("customers").cohort(1, owner_id=RowRef("c1")),
            ),
            "references auth.users, but ref\\('c1'\\) is a row of public.customers",
        ),
        (lambda s: s.table("customers").fill(2), "seeds none"),
        (lambda s: s.table("invoices").fill(2), "no parent row to reference"),
        (
            lambda s: (
                s.table("auth.users").fill(1),
                s.table("customers").fill(1),
                s.table("invoices").fill(1).per_parent("customers", count=1),
            ),
            "cannot also have cohorts",
        ),
        (
            lambda s: s.table("invoices").per_parent("customers", count=1),
            "has no rows in this scenario",
        ),
        (
            lambda s: (
                s.table("auth.users").fill(1),
                s.table("customers").fill(2),
                s.table("invoices").per_parent("customers", where={"tier": 1}, count=1),
            ),
            "tier is not pinned",
        ),
        (
            lambda s: (
                s.table("auth.users").fill(1),
                s.row("auth.users", name="bob"),
                s.table("customers").fill(2),
                s.table("invoices").per_parent("customers", where=RowRef("bob"), count=1),
            ),
            "must name a row of public.customers",
        ),
        (lambda s: s.table("auth.users").cohort(1, email="x@y"), "cannot pin columns"),
    ],
)
def test_invalid_scenarios_fail_at_compile_time(schema: SchemaInfo, build, match: str) -> None:
    _compile_error(schema, build, match)


def test_ambiguous_bare_names_ask_for_the_qualified_name() -> None:
    schema = parse_schema_sql(
        "CREATE TABLE a.items (id bigint PRIMARY KEY);CREATE TABLE b.items (id bigint PRIMARY KEY);"
    )
    _compile_error(schema, lambda s: s.table("items").fill(1), "ambiguous")


def test_a_composite_key_row_cannot_be_referenced() -> None:
    schema = parse_schema_sql("CREATE TABLE pairs (a bigint, b bigint, PRIMARY KEY (a, b));")
    _compile_error(schema, lambda s: s.row("pairs", name="p"), "no single-column primary key")


def test_several_foreign_keys_need_an_explicit_column() -> None:
    schema = parse_schema_sql(
        """
        CREATE TABLE people (id bigint PRIMARY KEY);
        CREATE TABLE loans (
          id bigint PRIMARY KEY,
          lender_id bigint NOT NULL REFERENCES people(id),
          borrower_id bigint NOT NULL REFERENCES people(id)
        );
        """
    )
    _compile_error(
        schema,
        lambda s: (s.table("people").fill(2), s.table("loans").per_parent("people", count=1)),
        "pass per_parent\\(..., column=...\\)",
    )

    @scenario(seed=3)
    def borrowers(s):
        s.table("people").fill(3)
        s.table("loans").per_parent("people", count=2, column="borrower_id")

    rows = _rows(borrowers.compile(schema), schema)["loans"]
    assert sorted(r["borrower_id"] for r in rows) == [1, 1, 2, 2, 3, 3]


def test_per_parent_where_a_named_row() -> None:
    schema = parse_schema_sql(
        """
        CREATE TABLE workspaces (id bigint PRIMARY KEY, title text NOT NULL);
        CREATE TABLE docs (
          id bigint PRIMARY KEY,
          workspace_id bigint NOT NULL REFERENCES workspaces(id)
        );
        """
    )

    @scenario(seed=5)
    def empty_workspace(s):
        s.row("workspaces", name="acme", title="Acme")
        empty = s.row("workspaces", name="initech", title="Initech")
        s.table("docs").per_parent("workspaces", where=empty, count=0, default=(2, 4))

    compiled = empty_workspace.compile(schema)
    rows = _rows(compiled, schema)
    assert {d["workspace_id"] for d in rows["docs"]} == {compiled.refs["acme"]}
    assert 2 <= len(rows["docs"]) <= 4


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: scenario(seed=True), "seed must be"),
        (lambda: tiered.spec and _builder().table("t").cohort(0), "positive int"),
        (lambda: _builder().table("t").fill(-1), "non-negative int"),
        (lambda: _builder().table("t").per_parent("p", count=(3, 1)), "min <= max"),
        (lambda: _builder().table("t").per_parent("p", count=True), "not a bool"),
        (
            lambda: _builder().table("t").per_parent("p", count=1).per_parent("p", count=1),
            "only one is supported",
        ),
        (lambda: _twice_named(), "used twice"),
        (lambda: _builder().row("t", name=""), "non-empty"),
    ],
)
def test_builder_rejects_bad_input_immediately(make, match: str) -> None:
    with pytest.raises(SqlProofScenarioError, match=match):
        result = make()
        if callable(result):
            result(lambda s: None)


def _builder():
    from sqlproof.scenarios.model import ScenarioBuilder

    return ScenarioBuilder()


def _twice_named():
    builder = _builder()
    builder.row("t", name="x")
    builder.row("u", name="x")


def test_repr_and_ref() -> None:
    assert repr(tiered) == "Scenario('tiered', seed=42)"
    assert repr(_builder().ref("alice")) == "ref('alice')"


def test_a_schema_that_models_auth_users_still_gets_derived_user_ids() -> None:
    """A schema file with an auth.users stub must not turn auth.users into
    a generated table: its ids come from the scenario, and a required
    foreign key into it counts the scenario's users as parents."""
    schema = parse_schema_sql(
        """
        CREATE TABLE auth.users (id uuid PRIMARY KEY, email text);
        CREATE TABLE members (
          id bigint PRIMARY KEY,
          user_id uuid NOT NULL REFERENCES auth.users(id)
        );
        """
    )

    @scenario(seed=4)
    def stubbed(s: ScenarioBuilder) -> None:
        alice = s.row("auth.users", name="alice")
        s.table("members").cohort(2, user_id=alice)

    compiled = stubbed.compile(schema)
    assert compiled.refs["alice"] == compiled.auth_users[0].id
    assert compiled.refs["alice"] != "00000000-0000-0000-0000-000000000001"
    assert "auth.users" not in compiled.sizes
    assert compiled.sizes == {"public.members": 2}
