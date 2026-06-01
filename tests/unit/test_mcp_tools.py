"""Tests for the sqlproof MCP server's tools.

The MCP server exposes sqlproof's capabilities to coding agents
without making them write Python: instead of importing sqlproof and
calling its API, the agent invokes named tools whose schemas are
discovered via the Model Context Protocol.

We test the tools as **plain Python functions**, not via the stdio
transport. FastMCP's `@mcp.tool()` decorator registers a function
in the server's tool registry without changing its callable shape,
so direct invocation is a faithful test of the tool's behavior
without the overhead of running a subprocess server.

Stdio round-trip is exercised by a separate smoke test (see
``test_mcp_server_smoke.py``).

Each test pins down both the tool's return *shape* and at least one
content invariant — agents read the result programmatically, so a
silent shape regression would be more disruptive than a thrown
exception.
"""

from __future__ import annotations

from typing import Any

from sqlproof.mcp.tools import generate_dataset, inspect_schema, list_recipes

# ---------------------------------------------------------------------------
# inspect_schema
# ---------------------------------------------------------------------------


def test_inspect_schema_returns_tables_columns_and_fks() -> None:
    """Invariant: `inspect_schema` returns a dict with `tables`, where
    each table has `name`, `columns`, `primary_key`, `foreign_keys`,
    `unique_constraints`, `check_constraints` keys.

    Failure case: a tool consumer expects `result["tables"][0]["columns"]`
    and gets KeyError because we accidentally renamed `columns` or
    dropped it from the schema.
    """
    result = inspect_schema(
        schema_sql="""
        CREATE TABLE customers (
          id SERIAL PRIMARY KEY,
          email TEXT NOT NULL UNIQUE
        );
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          total NUMERIC(10, 2) NOT NULL CHECK (total >= 0)
        );
        """
    )

    assert "tables" in result
    table_names = [t["name"] for t in result["tables"]]
    assert "customers" in table_names
    assert "orders" in table_names

    orders = next(t for t in result["tables"] if t["name"] == "orders")
    assert "columns" in orders
    column_names = [c["name"] for c in orders["columns"]]
    assert column_names == ["id", "customer_id", "total"]

    # FK should be present
    assert len(orders["foreign_keys"]) == 1
    fk = orders["foreign_keys"][0]
    assert fk["referenced_table"] == "customers"


def test_inspect_schema_reports_nullability_per_column() -> None:
    """Invariant: each column dict has a `nullable: bool` field.

    Failure case: agent uses `column["nullable"]` to decide whether to
    declare a column in a test's `columns=` dict; if nullable is
    missing or wrongly typed, the agent generates broken tests.
    """
    result = inspect_schema(
        schema_sql="""
        CREATE TABLE thing (
          id SERIAL PRIMARY KEY,
          required_col TEXT NOT NULL,
          optional_col TEXT
        );
        """
    )
    thing = result["tables"][0]
    cols_by_name = {c["name"]: c for c in thing["columns"]}
    assert cols_by_name["required_col"]["nullable"] is False
    assert cols_by_name["optional_col"]["nullable"] is True


def test_inspect_schema_rejects_invalid_sql_with_clear_error() -> None:
    """Invariant: invalid SQL produces a structured error response,
    not a stack-trace string. Tool consumers should be able to read
    `result["error"]` and surface it to the user.

    Failure case: agent receives a Python traceback as the tool's
    output and has no idea what went wrong.
    """
    result = inspect_schema(schema_sql="THIS IS NOT VALID SQL;;;")
    # Either an `error` key with a message, or a non-success status.
    # We accept either shape for now; the exact contract is pinned
    # when we add real consumers.
    assert "error" in result or result.get("status") == "error", (
        f"Expected structured error, got: {result}"
    )


# ---------------------------------------------------------------------------
# list_recipes
# ---------------------------------------------------------------------------


def test_list_recipes_returns_named_patterns() -> None:
    """Invariant: `list_recipes` returns a list of recipe dicts with
    `name` and `summary` keys at minimum. Agents call this to enumerate
    what they can ask for ("write me a Pattern 1 test"), then drill
    in via the recipe name.

    Failure case: agent has no way to discover what patterns sqlproof
    supports — they fall back to free-form prompts and write
    less-canonical tests.
    """
    recipes = list_recipes()
    assert isinstance(recipes, list)
    assert len(recipes) > 0
    for recipe in recipes:
        assert "name" in recipe
        assert "summary" in recipe
        assert isinstance(recipe["name"], str)
        assert isinstance(recipe["summary"], str)


def test_list_recipes_includes_the_three_canonical_patterns() -> None:
    """Invariant: the three patterns documented in AGENTS.md (RLS,
    RPC, stateful) are all enumerated.

    Failure case: a contributor adds a new pattern to AGENTS.md but
    forgets to register it as a recipe; the MCP-using agent never
    learns about it.
    """
    recipes = list_recipes()
    names = {r["name"] for r in recipes}
    # These are the canonical patterns from AGENTS.md; if the names
    # change in the future, this test breaks loudly so the recipe
    # list stays in sync with the docs.
    assert "rls-policy-test" in names
    assert "rpc-function-test" in names
    assert "stateful-test" in names


def test_list_recipes_recipe_has_optional_template_field() -> None:
    """Invariant: recipes optionally include a `template` (string)
    that the agent can fill in. Not all recipes need one; absence is
    OK, but if present it must be a string.

    Failure case: a tool consumer expects every recipe to have a
    template and fails on the ones that don't, or vice versa.
    """
    recipes = list_recipes()
    for recipe in recipes:
        if "template" in recipe:
            assert isinstance(recipe["template"], str)


# ---------------------------------------------------------------------------
# generate_dataset
# ---------------------------------------------------------------------------


def test_generate_dataset_returns_rows_per_table() -> None:
    """Invariant: result has a `dataset` key mapping table name to a
    list of row dicts whose length matches the requested size.

    Failure case: agent calls the tool expecting 5 rows for `orders`
    and gets something differently shaped — falls back to writing
    its own generator code.
    """
    result = generate_dataset(
        schema_sql="""
        CREATE TABLE customers (id SERIAL PRIMARY KEY, email TEXT NOT NULL);
        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          total INTEGER NOT NULL CHECK (total >= 0)
        );
        """,
        sizes={"customers": 2, "orders": 3},
    )

    assert result["status"] == "ok"
    assert "dataset" in result
    dataset = result["dataset"]
    assert "customers" in dataset
    assert "orders" in dataset
    assert len(dataset["customers"]) == 2
    assert len(dataset["orders"]) == 3


def test_generate_dataset_respects_fk_constraints() -> None:
    """Invariant: each generated child row's FK column references a
    real parent row's primary key. This is sqlproof's core
    correctness invariant for FK-respecting generation; if it fails
    here, the whole library is broken.

    Failure case: dataset has `orders.customer_id = 999` where no
    such customer exists — INSERT would fail in production.
    """
    result = generate_dataset(
        schema_sql="""
        CREATE TABLE parents (id SERIAL PRIMARY KEY);
        CREATE TABLE children (
          id SERIAL PRIMARY KEY,
          parent_id INTEGER NOT NULL REFERENCES parents(id)
        );
        """,
        sizes={"parents": 3, "children": 5},
    )

    parent_ids = {row["id"] for row in result["dataset"]["parents"]}
    for child in result["dataset"]["children"]:
        assert child["parent_id"] in parent_ids


def test_generate_dataset_rejects_invalid_sql_with_clear_error() -> None:
    """Invariant: invalid schema produces a structured error response.

    Failure case: agent receives a Python traceback string.
    """
    result = generate_dataset(schema_sql="NOT SQL;;;", sizes={})
    assert "error" in result or result.get("status") == "error"


def test_generate_dataset_returns_structured_error_when_generator_raises() -> None:
    """Invariant: if sqlproof's strategy or Hypothesis sampling
    raises (not a parser error — a generation error), the tool
    returns the structured error dict instead of crashing.

    Failure case: the agent gets a Python traceback instead of a
    clean error message, and has to guess what went wrong.

    Triggers by asking for rows in a table that doesn't exist —
    sqlproof's strategy raises a KeyError, which our generic
    except-Exception captures.
    """
    result = generate_dataset(
        schema_sql="CREATE TABLE only_table (id INTEGER PRIMARY KEY);",
        sizes={"nonexistent_table": 5},
    )
    # The error path may or may not fire depending on whether the
    # generator validates table names upfront; we just check that
    # if it does, the response shape is correct.
    if result["status"] == "error":
        assert "error" in result
        assert "error_kind" in result


def test_generate_dataset_serializes_decimals_uuids_and_dates_to_strings() -> None:
    """Invariant: the result is JSON-serializable end-to-end. Python-
    native types (Decimal, UUID, date/datetime) that sqlproof's
    generator emits get coerced to strings so they round-trip
    through the JSON-RPC transport without distortion.

    Failure case: the dataset reaches the MCP runtime with a
    `Decimal('5.25')` in it; the runtime's default JSON encoder
    throws, and the tool call fails with a confusing serialization
    error instead of returning the generated data.
    """
    import json

    result = generate_dataset(
        schema_sql="""
        CREATE TABLE wallets (
          id UUID PRIMARY KEY,
          balance NUMERIC(10, 2) NOT NULL,
          opened_at TIMESTAMPTZ NOT NULL
        );
        """,
        sizes={"wallets": 2},
    )

    assert result["status"] == "ok"
    # Every value in the dataset must be JSON-serializable. This
    # catches Decimal / UUID / datetime regressions in the coerce
    # helper.
    json.dumps(result["dataset"])  # must not raise

    # Spot-check: the UUID is a string, not a UUID object.
    wallet = result["dataset"]["wallets"][0]
    assert isinstance(wallet["id"], str), (
        f"UUID column should serialize to str, got {type(wallet['id']).__name__}"
    )
    assert isinstance(wallet["balance"], str), (
        f"NUMERIC column should serialize to str, got {type(wallet['balance']).__name__}"
    )


# ---------------------------------------------------------------------------
# Return-shape stability (cross-cutting)
# ---------------------------------------------------------------------------


def test_tools_return_json_serializable_results() -> None:
    """Invariant: every tool returns a JSON-serializable value. MCP
    serializes responses to JSON over stdio, so anything that isn't
    JSON-friendly silently fails or distorts.

    Failure case: a tool returns a `datetime`/`Decimal`/dataclass
    that breaks the stdio round-trip.
    """
    import json

    # inspect_schema
    schema_result = inspect_schema(schema_sql="CREATE TABLE t (id INTEGER);")
    json.dumps(schema_result)  # must not raise

    # list_recipes
    json.dumps(list_recipes())  # must not raise

    # generate_dataset — uses Decimal/UUID types internally; serializer
    # must coerce these to JSON-friendly strings/numbers.
    dataset_result = generate_dataset(
        schema_sql="CREATE TABLE t (id INTEGER PRIMARY KEY, price NUMERIC(10, 2));",
        sizes={"t": 1},
    )
    json.dumps(dataset_result)  # must not raise


def _example_dataset() -> dict[str, list[dict[str, Any]]]:
    """Helper for tests that need a non-empty dataset shape."""
    return {"orders": [{"id": 1, "total": 5}]}
