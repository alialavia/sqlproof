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
config::

    {
      "mcpServers": {
        "sqlproof": {
          "command": "sqlproof-mcp",
          "env": {
            "SQLPROOF_DATABASE_URL": "postgresql://..."
          }
        }
      }
    }

Optional install: ``pip install sqlproof[mcp]``.

This module imports lazily inside ``server.main()`` so callers of
``sqlproof.mcp.tools`` (which don't need the runtime) aren't forced
to install the ``mcp`` SDK.
"""

from __future__ import annotations
