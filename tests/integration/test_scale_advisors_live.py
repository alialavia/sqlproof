"""Scale follow-up 6, live: `index_advisor` names the missing index.

`invoices.customer_id` is a foreign key with no index, so counting one
customer's invoices scans the whole table -- linear, and fixed by one
index. The sweep's advisor step must rank exactly that index first, in
the result and in the saved artifact.

Runs in a database of its own, created and dropped here, because it
installs the `index_advisor` extension (with `hypopg`) -- which this
non-superuser role can do on the Supabase image: both are trusted
extensions there -- and the shared test database must not gain it.
"""
from __future__ import annotations

import json
import os
from collections.abc import Generator
from uuid import uuid4

import pytest

from sqlproof import SqlProof, _dsn
from sqlproof import _database as database
from sqlproof._dsn import quote_identifier
from sqlproof.config import SqlProofConfig
from sqlproof.scale import heaviest, scale_analysis
from sqlproof.scale.advisors import NOT_INSTALLED, OUTER_CALL_ONLY

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE adv.customers (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE adv.invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES adv.customers (id),
  amount integer NOT NULL
);
CREATE FUNCTION adv.invoice_count(c bigint) RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM adv.invoices WHERE customer_id = c;
  RETURN n;
END $$;
"""

INDEX = "CREATE INDEX ON adv.invoices USING btree (customer_id)"


@pytest.fixture(scope="module")
def dsn() -> Generator[str]:
    base_dsn = os.environ[DSN_ENV]
    with database.connect(base_dsn, autocommit=True) as admin:
        available = admin.execute(
            "SELECT count(*) FROM pg_available_extensions "
            "WHERE name IN ('index_advisor', 'hypopg')"
        ).fetchone()[0]
    if available != 2:
        pytest.skip("this server does not ship index_advisor and hypopg (the Supabase image does)")
    db_name = f"sqlproof_advisors_{uuid4().hex[:12]}"
    with database.connect(base_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {quote_identifier(db_name)}")
    try:
        parts = _dsn.conninfo_to_dict(base_dsn)
        parts["dbname"] = db_name
        db_dsn = _dsn.make_conninfo(**parts)
        with database.connect(db_dsn, autocommit=True) as connection:
            connection.execute("CREATE SCHEMA adv")
            connection.execute(SCHEMA_SQL)
        yield db_dsn
    finally:
        with database.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {quote_identifier(db_name)} WITH (FORCE)")


def _extension(dsn: str, *, installed: bool) -> None:
    with database.connect(dsn, autocommit=True) as connection:
        if installed:
            connection.execute("CREATE EXTENSION IF NOT EXISTS index_advisor CASCADE")
        else:
            connection.execute("DROP EXTENSION IF EXISTS index_advisor")
            connection.execute("DROP EXTENSION IF EXISTS hypopg")


def _scale(dsn: str, tmp_path, **kwargs):
    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema="adv"))
    return scale_analysis(
        proof, "adv.invoice_count",
        sizes={"customers": 20, "invoices": 1000},
        args=[heaviest("adv.customers.id")],
        max_factor=16, artifact_dir=tmp_path, advisors=True, **kwargs,
    )


def test_the_unindexed_foreign_key_is_the_first_advisor_hit(dsn, tmp_path):
    _extension(dsn, installed=True)
    result = _scale(dsn, tmp_path, nested_plans=True)

    assert result.advisors
    first = result.advisor_hits[0]
    assert (first.rank, first.relation, first.index_statement) == (1, "adv.invoices", INDEX)
    assert "adv.invoices WHERE customer_id = ($1::bigint)" in first.statement
    assert first.total_cost_after < first.total_cost_before
    # The function is still measured as it is: one seq scan, linear.
    assert 0.7 < result.exponent < 1.3

    (artifact,) = tmp_path.glob("*.json")
    data = json.loads(artifact.read_text())
    saved = data["advisor_hits"][0]
    assert (saved["rank"], saved["source"], saved["relation"], saved["index_statement"]) == (
        1, "index_advisor", "adv.invoices", INDEX,
    )
    assert data["advisors"] is True


def test_without_nested_plans_the_outer_call_alone_is_advised(dsn, tmp_path):
    """A PL/pgSQL function is never inlined: the outer `SELECT fn($1)`
    gives index_advisor nothing to see, and the notes say why."""
    _extension(dsn, installed=True)
    result = _scale(dsn, tmp_path)
    assert list(result.advisor_hits) == []
    assert OUTER_CALL_ONLY.format(function="adv.invoice_count") in result.advisor_notes


def test_a_missing_extension_is_recorded_and_the_measurement_stands(dsn, tmp_path):
    _extension(dsn, installed=False)
    result = _scale(dsn, tmp_path, nested_plans=True)
    assert list(result.advisor_hits) == []
    assert list(result.advisor_notes) == [NOT_INSTALLED]
    assert 0.7 < result.exponent < 1.3
    with database.connect(dsn, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_extension WHERE extname = 'index_advisor'"
        ).fetchone()[0] == 0, "the advisor step must never create the extension"
    (artifact,) = tmp_path.glob("*.json")
    assert json.loads(artifact.read_text())["advisor_notes"] == [NOT_INSTALLED]
