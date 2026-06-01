"""MCP (Model Context Protocol) server for sqlproof.

Lets coding agents (Claude Code, Cursor, Cline, Claude Desktop, anything
speaking MCP) invoke sqlproof's capabilities as callable tools instead
of writing Python code that imports the library. Reduces the friction
of "did I import this right?" and "what's the right keyword argument?"
that LLM-generated test code regularly hits.

Tools exposed in v1 (see ``sqlproof.mcp.tools``):

- ``inspect_schema(schema_sql)`` — parse a SQL schema, return tables,
  columns, FKs, constraints as JSON.
- ``generate_dataset(schema_sql, sizes)`` — produce a single example
  dataset that respects every FK/CHECK/UNIQUE/NOT NULL.
- ``check_property(schema_sql, property_sql, sizes, runs)`` — run a
  property against generated datasets, return ok or counterexample.
- ``list_recipes()`` — enumerate the named test patterns (RLS, RPC,
  stateful) the agent can ask for.

The server transport is stdio. Users add it to their MCP client
config (Claude Desktop, Cursor, Cline, Claude Code, etc.) via
``uvx``, which downloads and runs ``sqlproof[mcp]`` on demand —
no separate install step required::

    {
      "mcpServers": {
        "sqlproof": {
          "command": "uvx",
          "args": ["--from", "sqlproof[mcp]", "sqlproof-mcp"]
        }
      }
    }

Pin a specific version for reproducibility
(``--from sqlproof[mcp]==0.2.4``) or omit the version for
always-latest. First invocation downloads (~10s); subsequent calls
hit the uv cache.

This config works regardless of whether the user has sqlproof in
any project venv — ``uvx`` runs the server in its own ephemeral
environment.

For Python-package callers (notebooks, custom CLI scripts) who
need the tool LOGIC without the MCP runtime, import
``sqlproof.mcp.tools`` directly — those functions don't depend on
the ``mcp`` SDK and ship with the base ``sqlproof`` install.
"""

from __future__ import annotations
