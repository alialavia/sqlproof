"""Scenarios: named, seeded, reproducible database states.

A scenario pins the rows that matter for a case and lets the generator
fill everything else from a seed. The same scenario, seed, schema and
sqlproof version always load identical rows.

Experimental: the API may change in a minor release.
"""

from __future__ import annotations

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scenarios.model import (
    DEFAULT_CLOCK,
    RowRef,
    Scenario,
    ScenarioBuilder,
    TableBuilder,
    scenario,
)

__all__ = [
    "DEFAULT_CLOCK",
    "RowRef",
    "Scenario",
    "ScenarioBuilder",
    "SqlProofScenarioError",
    "TableBuilder",
    "scenario",
]
