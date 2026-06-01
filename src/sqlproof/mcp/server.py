"""FastMCP server wiring for sqlproof.

This module is the entry point for the ``sqlproof-mcp`` console
script registered in ``pyproject.toml``. It instantiates a FastMCP
server, registers each tool from ``sqlproof.mcp.tools`` via the
``@mcp.tool()`` decorator, and runs the stdio transport.

The tool LOGIC lives in ``sqlproof.mcp.tools`` (callable from
ordinary Python) — this file is just the MCP runtime adapter. The
split keeps unit tests independent of the ``mcp`` SDK and makes the
tools reusable from notebooks / CLI in the future.

End-user install — the canonical recipe uses ``uvx``, which
downloads and runs ``sqlproof[mcp]`` on demand without a separate
install step. Add to your MCP client config (Claude Desktop,
Cursor, Cline, Claude Code, etc.)::

    {
      "mcpServers": {
        "sqlproof": {
          "command": "uvx",
          "args": ["--from", "sqlproof[mcp]", "sqlproof-mcp"]
        }
      }
    }

Pin the version for reproducibility::

    "args": ["--from", "sqlproof[mcp]==0.2.4", "sqlproof-mcp"]

This works whether or not the user has sqlproof installed in any
project venv — ``uvx`` runs the server in its own ephemeral
environment.

Local development from a sqlproof checkout (this repo) uses the
standard venv pattern::

    uv sync --extra dev
    uv run sqlproof-mcp

The server runs in the foreground and reads MCP protocol messages
on stdin / stdout; press Ctrl-C to stop.
"""

from __future__ import annotations

from typing import Any

from sqlproof.mcp import tools


def _build_server() -> Any:
    """Build the FastMCP server.

    Lazy-imports ``mcp.server.fastmcp`` so tests of the tool logic
    don't pay the SDK import cost. Returns ``Any`` because FastMCP's
    type stubs aren't always available across versions; mypy treats
    it as opaque.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "sqlproof",
        instructions=(
            "sqlproof — property-based testing for PostgreSQL. "
            "Use `inspect_schema` to parse a SQL schema into a structured "
            "JSON representation (tables, columns, FKs, constraints) before "
            "deciding what tests to write. Use `generate_dataset` to "
            "produce one valid example dataset that respects every "
            "FK/CHECK/UNIQUE/NOT NULL in the schema. Use `list_recipes` "
            "to discover the canonical test patterns (RLS, RPC, stateful) "
            "and get template code for each."
        ),
    )

    # Register tools. The decorators inspect the function signatures
    # to build JSON schemas, so the function definitions in
    # `sqlproof.mcp.tools` are the source of truth for the tool
    # surface — including type hints and docstrings.
    # The inner wrapper functions below are 1-line delegations to the
    # tested logic in `sqlproof.mcp.tools`. They exist only to give
    # FastMCP a function it can decorate (the SDK doesn't currently
    # expose a programmatic registration API). Their bodies aren't
    # exercised by unit tests — testing them would require driving
    # the MCP runtime via stdio, which is covered by the manual
    # `sqlproof-mcp` smoke run, not unit tests. Hence `pragma: no cover`.

    @mcp.tool()
    def inspect_schema(schema_sql: str) -> dict[str, Any]:  # pragma: no cover
        """Parse a SQL schema and return its structure as JSON.

        Use BEFORE writing tests to understand what tables, columns,
        FKs, and constraints exist. The agent that knows the schema
        shape can write tests that respect it; one that guesses
        produces broken tests.
        """
        return tools.inspect_schema(schema_sql=schema_sql)

    @mcp.tool()
    def generate_dataset(  # pragma: no cover
        schema_sql: str, sizes: dict[str, int]
    ) -> dict[str, Any]:
        """Generate one example dataset that respects every constraint.

        `sizes` is a mapping of table name to row count. The
        generator picks values that satisfy FK references, CHECK
        constraints, UNIQUE constraints (including composite), and
        NOT NULL columns. Useful for seeding a dev DB or for
        previewing what shapes sqlproof's property tests will
        explore.
        """
        return tools.generate_dataset(schema_sql=schema_sql, sizes=sizes)

    @mcp.tool()
    def list_recipes() -> list[dict[str, Any]]:  # pragma: no cover
        """Enumerate sqlproof's canonical test patterns.

        Returns a list of recipes with `name`, `summary`, and (where
        applicable) a `template` Python snippet. Three patterns
        ship in v1: rls-policy-test, rpc-function-test, stateful-test.
        """
        return tools.list_recipes()

    return mcp


def main() -> None:  # pragma: no cover — invokes stdio runtime; covered by manual smoke run
    """Run the MCP server on stdio.

    Invoked by the ``sqlproof-mcp`` console-script entry point. The
    server reads from stdin and writes to stdout; any logging goes
    to stderr so it doesn't corrupt the protocol stream.
    """
    server = _build_server()
    # FastMCP's `.run()` defaults to stdio when called without args.
    # Explicit kwarg so a future SDK default-change doesn't surprise us.
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
