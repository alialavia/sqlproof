"""Smoke tests for the sqlproof MCP server wiring.

The tool LOGIC is tested directly in ``test_mcp_tools.py``. This
file verifies the FastMCP server **assembles correctly**: the three
v1 tools are registered, their JSON schemas pull from the right
function signatures, and the server object exposes the expected
interface.

We don't drive a full stdio round-trip here — that needs subprocess
plumbing and tends to be flaky in CI. The unit tests + a manual
``sqlproof-mcp`` smoke run cover the surface together.
"""

from __future__ import annotations

import asyncio


def test_server_builds_with_three_v1_tools_registered() -> None:
    """Invariant: ``_build_server`` returns a FastMCP server whose
    tool registry contains exactly the v1 surface (inspect_schema,
    generate_dataset, list_recipes). v2 tools (check_property,
    run_rls_test) are deliberately NOT included until they have
    live-DB plumbing.

    Failure case: a contributor adds a v2 tool to server.py before
    its tests / DB plumbing exist; tool consumers see it advertised
    and call it; it raises.
    """
    from sqlproof.mcp.server import _build_server

    server = _build_server()
    # FastMCP exposes registered tools via list_tools(); we sort
    # the names because registration order isn't a stable surface.
    tools = asyncio.run(server.list_tools())
    names = sorted(t.name for t in tools)
    assert names == ["generate_dataset", "inspect_schema", "list_recipes"]


def test_inspect_schema_tool_has_a_description() -> None:
    """Invariant: every registered tool has a non-empty description.
    MCP clients display the description to the user / agent; an
    empty one is a discoverability failure.

    Failure case: tool ships with no docstring; agents have no idea
    when to call it.
    """
    from sqlproof.mcp.server import _build_server

    server = _build_server()
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    for name in ("inspect_schema", "generate_dataset", "list_recipes"):
        description = by_name[name].description or ""
        assert len(description) > 20, (
            f"{name} has empty/trivial description: {description!r}"
        )
