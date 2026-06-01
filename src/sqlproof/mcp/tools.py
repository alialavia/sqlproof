"""Tool implementations for the sqlproof MCP server.

Each tool is a plain Python function — the ``@mcp.tool()`` decorator
in ``sqlproof.mcp.server`` registers them with the MCP runtime. We
keep the tool LOGIC here, separate from the server wiring, so:

  - Unit tests can call the functions directly (no stdio overhead).
  - The functions can be reused from other entry points (CLI,
    notebooks, etc.) without forcing the ``mcp`` dependency.
  - The schema discovery FastMCP does (type hints → JSON schema)
    works against clean function signatures, not server internals.

Every tool returns a **JSON-serializable** value. The MCP runtime
serializes return values over stdio; non-serializable values
(datetime, Decimal, dataclass) would either fail or distort
silently. Tests in ``test_mcp_tools.py`` enforce this with a
round-trip ``json.dumps`` check.

Error handling: tools return structured ``{"error": str, ...}``
dicts for predictable failure modes (invalid SQL, missing DSN,
etc.). They only raise for programming errors (caller passed the
wrong type), which the MCP runtime surfaces as a tool-execution
error.
"""

from __future__ import annotations

from typing import Any

from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.model import (
    CheckConstraint,
    Column,
    ForeignKey,
    Table,
)
from sqlproof.schema.parse_sql import parse_schema_sql


def inspect_schema(*, schema_sql: str) -> dict[str, Any]:
    """Parse a SQL schema string and return its structure as JSON.

    The agent uses this to discover what tables exist, what columns
    each has, and what constraints (FKs, UNIQUEs, CHECKs) apply —
    before deciding what tests to write.

    Returns a dict shaped like::

        {
            "tables": [
                {
                    "schema": "public",
                    "name": "orders",
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False, ...},
                        ...
                    ],
                    "primary_key": ["id"],
                    "foreign_keys": [
                        {"columns": ["customer_id"], "referenced_table": "customers",
                         "referenced_columns": ["id"]},
                        ...
                    ],
                    "unique_constraints": [...],
                    "check_constraints": [...]
                },
                ...
            ]
        }

    On invalid SQL, returns ``{"error": str, "status": "error"}``
    instead of raising — agents shouldn't have to handle Python
    exceptions over the JSON-RPC transport.
    """
    try:
        schema = parse_schema_sql(schema_sql, schema="public")
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }

    return {
        "status": "ok",
        "tables": [_serialize_table(t) for t in schema.tables],
    }


def generate_dataset(
    *,
    schema_sql: str,
    sizes: dict[str, int],
) -> dict[str, Any]:
    """Generate one valid dataset from a schema + row-count map.

    Useful when the agent wants to *see* what shapes sqlproof will
    feed into property tests, or wants to seed a dev database. The
    returned dataset respects every FK / CHECK / UNIQUE / NOT NULL
    in the schema (that's sqlproof's core correctness contract;
    tests in this repo's main suite cover it).

    Returns::

        {
            "status": "ok",
            "dataset": {"customers": [{...}], "orders": [{...}, ...]}
        }

    On invalid schema, returns ``{"status": "error", "error": ...}``.

    Notes:

    - Decimals, UUIDs, and dates are serialized as strings so the
      result round-trips through JSON cleanly.
    - This produces ONE example. For property testing across many
      shapes, use ``check_property`` (which runs the agent's property
      against many generated examples and returns a counterexample
      on failure).
    """
    try:
        schema = parse_schema_sql(schema_sql, schema="public")
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }

    try:
        strategy = dataset_strategy(schema, sizes=sizes)
        dataset = draw_example(strategy)
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "error_kind": type(exc).__name__,
        }

    return {
        "status": "ok",
        "dataset": _serialize_dataset(dataset),
    }


def list_recipes() -> list[dict[str, Any]]:
    """Enumerate the named test patterns sqlproof recommends.

    Agents call this to discover what's available, then ask for a
    specific recipe by name (e.g. "give me the rls-policy-test
    recipe"). The recipe content lives in this module (rather than
    being scraped from AGENTS.md at runtime) so the format is stable
    even if AGENTS.md is rewritten.

    Each recipe has at minimum ``name`` and ``summary``. Optionally
    ``template`` (a Python code snippet the agent can fill in with
    schema/table/policy names).
    """
    return [
        {
            "name": "rls-policy-test",
            "summary": (
                "Property-based test that an RLS policy correctly gates "
                "access. Generates rows, then for each (user, row) pair "
                "verifies the policy returns the expected visibility. "
                "Always tests BOTH directions (owner can see, non-owner "
                "cannot) — a policy that returns too much is the actual "
                "bug class."
            ),
            "template": (
                "from hypothesis import given\n"
                "from hypothesis import strategies as st\n\n"
                "from sqlproof import SqlProof\n"
                "from sqlproof.contrib.supabase import as_supabase_user\n\n\n"
                "@given(data=st.data())\n"
                "def test_owner_can_read_their_own_<resource>(\n"
                "    supabase_proof: SqlProof, data,\n"
                ") -> None:\n"
                "    dataset = data.draw(supabase_proof.dataset_strategy(\n"
                "        sizes={'<resource_table>': 1},\n"
                "    ))\n"
                "    with supabase_proof.client_for_dataset(dataset) as db:\n"
                "        resource = dataset['<resource_table>'][0]\n"
                "        with as_supabase_user(db, resource['user_id']):\n"
                "            rows = db.query(\n"
                "                'SELECT id FROM <resource_table> WHERE id = %s',\n"
                "                resource['id'],\n"
                "            )\n"
                "        assert len(rows) == 1\n"
            ),
        },
        {
            "name": "rpc-function-test",
            "summary": (
                "Property test for a public SQL function / RPC. For "
                "deterministic functions: generate inputs, assert "
                "invariants (non-negative, monotonic, etc.). For "
                "aggregating functions: generate a dataset and reconcile "
                "the DB-side result against a Python recomputation."
            ),
            "template": (
                "from hypothesis import HealthCheck, given, settings\n"
                "from hypothesis import strategies as st\n\n"
                "from sqlproof.client import SqlProofClient\n\n\n"
                "PROOF_KW = settings(\n"
                "    max_examples=100,\n"
                "    deadline=None,\n"
                "    suppress_health_check=[HealthCheck.function_scoped_fixture],\n"
                ")\n\n\n"
                "@PROOF_KW\n"
                "@given(subtotal=st.decimals(min_value=0, max_value=10000, places=2))\n"
                "def test_<function>_is_never_negative(db: SqlProofClient, subtotal):\n"
                "    result = db.scalar('SELECT <function>(%s::numeric)', subtotal)\n"
                "    assert result >= 0\n"
            ),
        },
        {
            "name": "stateful-test",
            "summary": (
                "Stateful test for bugs that only manifest after a "
                "sequence of operations (membership churn, pagination, "
                "accumulation). Subclass SqlProofStateMachine, define "
                "@rule and @invariant decorators, run via "
                "proof.run_state_machine. Slower than property tests — "
                "use when a single example can't reproduce the bug."
            ),
            "template": (
                "from hypothesis.stateful import invariant, rule\n"
                "from hypothesis import strategies as st\n\n"
                "from sqlproof import SqlProof\n"
                "from sqlproof.testing import SqlProofStateMachine\n\n\n"
                "class <Subject>Machine(SqlProofStateMachine):\n"
                "    def on_setup(self) -> None:\n"
                "        # Initialize per-example state\n"
                "        ...\n\n"
                "    @rule(arg=st.integers(0, 10))\n"
                "    def <do_thing>(self, arg):\n"
                "        # Mutate state; track in self for invariants\n"
                "        ...\n\n"
                "    @invariant()\n"
                "    def <thing_is_consistent>(self) -> None:\n"
                "        # Assert DB state matches Python model\n"
                "        ...\n\n\n"
                "def test_<subject>_invariant(proof: SqlProof) -> None:\n"
                "    proof.run_state_machine(<Subject>Machine)\n"
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_table(table: Table) -> dict[str, Any]:
    return {
        "schema": table.schema,
        "name": table.name,
        "columns": [_serialize_column(c) for c in table.columns],
        "primary_key": list(table.primary_key),
        "foreign_keys": [_serialize_foreign_key(fk) for fk in table.foreign_keys],
        "unique_constraints": [list(u) for u in table.unique_constraints],
        "check_constraints": [_serialize_check_constraint(c) for c in table.check_constraints],
    }


def _serialize_column(column: Column) -> dict[str, Any]:
    return {
        "name": column.name,
        "type": column.type.name,
        "nullable": column.nullable,
        "default": column.default,
        "is_generated": column.is_generated,
    }


def _serialize_foreign_key(fk: ForeignKey) -> dict[str, Any]:
    return {
        "columns": list(fk.columns),
        "referenced_schema": fk.referenced_schema,
        "referenced_table": fk.referenced_table,
        "referenced_columns": list(fk.referenced_columns),
        "on_delete": fk.on_delete,
        "on_update": fk.on_update,
    }


def _serialize_check_constraint(check: CheckConstraint) -> dict[str, Any]:
    return {
        "expression": check.expression,
    }


def _serialize_dataset(
    dataset: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Coerce dataset values to JSON-friendly shapes.

    sqlproof's row generator produces Python-native types (Decimal,
    UUID, datetime). MCP serializes return values to JSON over stdio,
    so we coerce here to keep the surface stable across tool
    consumers.
    """
    from datetime import date, datetime, time
    from decimal import Decimal
    from uuid import UUID

    def coerce(value: Any) -> Any:
        if isinstance(value, (Decimal, UUID)):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, bytes):  # pragma: no cover — sqlproof's row generator doesn't emit BYTEA columns yet; this branch reserves the encoding for when it does
            # base64-encode binary so the JSON round-trips reversibly
            import base64

            return {"__type__": "bytes", "value": base64.b64encode(value).decode("ascii")}
        return value

    return {
        table_name: [{col: coerce(v) for col, v in row.items()} for row in rows]
        for table_name, rows in dataset.items()
    }
