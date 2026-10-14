"""The MCP scenario tools, without a database.

The live load path is covered in tests/integration/test_scenarios_live.py.
"""

from __future__ import annotations

import json
from typing import Any

from sqlproof.mcp.tools import _dsn_secrets, compile_scenario, load_scenario

SCHEMA_SQL = """
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3)
);
CREATE TABLE invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id)
);
"""

SCENARIO: dict[str, Any] = {
    "version": 1,
    "name": "tiered",
    "seed": 4,
    "tables": {
        "customers": [{"row": "acme", "set": {"tier": 1}}, {"cohort": 2, "set": {"tier": 2}}],
        "invoices": [{"per_parent": {"parent": "customers", "where": {"ref": "acme"}, "count": 3}}],
    },
}


def test_compile_scenario_reports_rows_and_refs() -> None:
    result = compile_scenario(scenario=SCENARIO, schema_sql=SCHEMA_SQL)
    assert result["status"] == "ok"
    assert result["rows"] == {"public.customers": 3, "public.invoices": 3}
    assert result["refs"] == {"acme": 1}
    assert result["scenario"] == "tiered"
    assert len(result["fingerprint"]) == 64
    json.dumps(result)  # must not raise


def test_compile_scenario_reports_auth_users_and_stringifies_uuid_refs() -> None:
    schema = "CREATE TABLE auth.users (id uuid PRIMARY KEY, email text);"
    scenario = {"version": 1, "name": "u", "seed": 1, "tables": {"auth.users": [{"row": "a"}]}}
    result = compile_scenario(scenario=scenario, schema_sql=schema)
    assert result["rows"] == {"auth.users": 1}
    assert isinstance(result["refs"]["a"], str)
    json.dumps(result)


def test_compile_scenario_returns_problems_as_errors() -> None:
    bad = {**SCENARIO, "tables": {"customers": [{"cohort": 1, "set": {"tier": 9}}]}}
    result = compile_scenario(scenario=bad, schema_sql=SCHEMA_SQL)
    assert result["status"] == "error"
    assert "violates CHECK" in result["error"]
    assert result["error_kind"] == "SqlProofScenarioError"

    malformed = compile_scenario(scenario={"version": 2}, schema_sql=SCHEMA_SQL)
    assert malformed["status"] == "error"
    assert "$.version" in malformed["error"]


def test_load_scenario_never_echoes_the_password() -> None:
    dsn = "postgresql://someone:hunter2-secret@127.0.0.1:1/nowhere?connect_timeout=1"
    result = load_scenario(scenario=SCENARIO, dsn=dsn)
    assert result["status"] == "error"
    assert "hunter2-secret" not in json.dumps(result)


def test_load_scenario_rejects_a_malformed_scenario_before_connecting() -> None:
    result = load_scenario(scenario={"version": 1}, dsn="postgresql://x@127.0.0.1:1/db")
    assert result["status"] == "error"
    assert "$.name" in result["error"]


def test_dsn_secrets_cover_uri_and_keyword_forms() -> None:
    assert _dsn_secrets("postgresql://u:pw@h/db") == ("postgresql://u:pw@h/db", "pw")
    assert _dsn_secrets("host=h password='p w' dbname=d") == (
        "host=h password='p w' dbname=d",
        "p w",
    )
    assert _dsn_secrets("not a dsn ' at all") == ("not a dsn ' at all",)
