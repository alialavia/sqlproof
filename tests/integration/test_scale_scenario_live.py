"""`scale_analysis(scenario=...)` against a real database.

The scenario fixes a named customer and grows everything else; the
function is called with that customer's key, resolved at every factor.
"""

from __future__ import annotations

import json
import os

import pytest

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.config import SqlProofConfig
from sqlproof.scale import scale_analysis
from sqlproof.scenarios import RowRef, ScenarioBuilder, scenario

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE customers (id bigint PRIMARY KEY, tier int NOT NULL);
CREATE TABLE invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10, 2) NOT NULL
);
"""


@scenario(seed=21)
def growing_acme(s: ScenarioBuilder) -> None:
    acme = s.row("customers", name="acme", tier=1)
    s.table("customers").cohort(2, tier=2).fill(200)
    # acme's invoices grow with the factor, and so do everyone else's.
    s.table("invoices").per_parent("customers", where=acme, count=1000, default=(0, 6))


@pytest.fixture
def proof():
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS scale_scenario CASCADE")
        try:
            connection.execute("CREATE SCHEMA scale_scenario")
            connection.execute("SET search_path TO scale_scenario")
            connection.execute(SCHEMA_SQL)
            connection.execute(
                "CREATE FUNCTION scale_scenario.invoice_total(c bigint) RETURNS numeric "
                "LANGUAGE sql STABLE AS "
                "$$ SELECT coalesce(sum(total), 0) FROM scale_scenario.invoices "
                "WHERE customer_id = c $$"
            )
            yield SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema="scale_scenario")
            )
        finally:
            connection.execute("DROP SCHEMA IF EXISTS scale_scenario CASCADE")


def test_a_scenario_sweep_resolves_refs_and_records_the_scenario(proof, tmp_path):
    result = scale_analysis(
        proof,
        "scale_scenario.invoice_total",
        scenario=growing_acme,
        args=[RowRef("acme")],
        max_factor=16,
        artifact_dir=tmp_path,
    )
    # No index on customer_id: a seq scan over invoices, which grow linearly.
    assert 0.7 < result.exponent < 1.3
    assert all(point.args == (1,) for point in result.points)  # acme's key, every factor
    totals = [point.total_rows for point in result.points]
    assert totals == sorted(totals) and totals[0] < totals[-1]
    assert result.scenario == {
        "name": "growing_acme",
        "fingerprint": growing_acme.fingerprint,
        "scale_cohorts": False,
    }
    assert result.seed == 21
    [artifact] = list(tmp_path.glob("*.json"))
    saved = json.loads(artifact.read_text())
    assert saved["scenario"]["name"] == "growing_acme"
    assert saved["argument_policy"] == [{"kind": "ref", "name": "acme"}]
    with database.connect(os.environ[DSN_ENV]) as conn:
        left = conn.execute("SELECT count(*) FROM scale_scenario.invoices").fetchone()
    assert left == (0,)  # the sweep empties what it loaded


def test_a_scenario_sweep_removes_the_auth_users_it_seeded(tmp_path):
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        has_auth = connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'auth' AND table_name = 'users'"
        ).fetchone()
        if has_auth is None:
            pytest.skip("needs a Supabase-shaped database with auth.users")
        connection.execute("DROP SCHEMA IF EXISTS scale_auth CASCADE")
        connection.execute("CREATE SCHEMA scale_auth")
        connection.execute(
            "CREATE TABLE scale_auth.notes (id bigint PRIMARY KEY, "
            "owner uuid NOT NULL REFERENCES auth.users (id), body text)"
        )
        connection.execute(
            "CREATE FUNCTION scale_auth.note_count() RETURNS bigint LANGUAGE sql STABLE AS "
            "$$ SELECT count(*) FROM scale_auth.notes $$"
        )
        before = connection.execute("SELECT count(*) FROM auth.users").fetchone()

    @scenario(seed=3)
    def owners(s: ScenarioBuilder) -> None:
        s.table("auth.users").fill(5)
        s.table("notes").fill(400)

    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema="scale_auth"))
    # A user the scenario would seed already exists (say, from an earlier
    # `scenarios up`): the sweep must leave it alone.
    existing = owners.compile(proof.schema_info).auth_users[0]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO auth.users (id, email) VALUES (%s, %s)", [existing.id, existing.email]
        )
    try:
        result = scale_analysis(
            proof, "scale_auth.note_count", scenario=owners, max_factor=8, artifact_dir=None
        )
        assert result.points
        with database.connect(dsn) as conn:
            after = conn.execute("SELECT count(*) FROM auth.users").fetchone()
            kept = conn.execute(
                "SELECT count(*) FROM auth.users WHERE id = %s", [existing.id]
            ).fetchone()
        assert after == (before[0] + 1,)
        assert kept == (1,)
    finally:
        with database.connect(dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS scale_auth CASCADE")
            connection.execute("DELETE FROM auth.users WHERE id = %s", [existing.id])


def test_a_scenario_strategy_drives_a_property_test(proof):
    from hypothesis import HealthCheck, given, settings

    # Property tests want small datasets: Hypothesis draws row by row.
    @scenario(seed=4)
    def small_acme(s: ScenarioBuilder) -> None:
        acme = s.row("customers", name="acme", tier=1)
        s.table("customers").fill(5)
        s.table("invoices").per_parent("customers", where=acme, count=1000 // 100, default=(0, 2))

    seen: list[int] = []

    @settings(max_examples=10, deadline=None, suppress_health_check=list(HealthCheck))
    @given(dataset=small_acme.strategy(proof))
    def property_holds(dataset):
        with proof.client_for_dataset(dataset) as db:
            acme_invoices = db.query(
                "SELECT count(*) AS n FROM scale_scenario.invoices WHERE customer_id = 1"
            )[0]["n"]
            seen.append(acme_invoices)
            assert acme_invoices == 10

    property_holds()
    assert len(seen) == 10
