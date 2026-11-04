"""Growing chosen tables of a scenario in a scale sweep.

By default a sweep multiplies every fill. A function's cost usually grows
with one dimension -- prompts, content pages, brands -- so `grow` names
the tables to multiply and keeps the rest at their size, the way the
incident that motivated a measurement actually grew.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofScenarioError, SqlProofUsageError
from sqlproof.scale.sweep import ScenarioProfile, run_sweep
from sqlproof.scenarios import Scenario, ScenarioBuilder
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = parse_schema_sql(
    """
    CREATE TABLE projects (id bigint PRIMARY KEY, name text);
    CREATE TABLE prompts (
      id bigint PRIMARY KEY, project_id bigint NOT NULL REFERENCES projects(id), kind text
    );
    CREATE TABLE pages (id bigint PRIMARY KEY, project_id bigint NOT NULL REFERENCES projects(id));
    CREATE TABLE responses (
      id bigint PRIMARY KEY, prompt_id bigint NOT NULL REFERENCES prompts(id)
    );
    CREATE TABLE contexts (
      id bigint PRIMARY KEY, response_id bigint NOT NULL REFERENCES responses(id)
    );
    """
)


def _scenario(
    prompts: int, cohort: int, pages: int, responses: int, per: tuple[int, int]
) -> Scenario:
    def build(s: ScenarioBuilder) -> None:
        acme = s.row("projects", name="acme")
        s.table("prompts").fill(prompts, project_id=acme)
        s.table("prompts").cohort(cohort, project_id=acme, kind="branded")
        s.table("pages").fill(pages, project_id=acme)
        s.table("responses").fill(responses)
        s.table("contexts").per_parent("responses", count=per)

    return Scenario(build, seed=5, name="grow")


def _sizes(built: Scenario) -> dict[str, int]:
    return {name.split(".")[-1]: size for name, size in built.compile(SCHEMA).sizes.items()}


def _counts(built: Scenario) -> dict[str, list[tuple[str, int]]]:
    return {
        table.name: [(group.kind, group.count) for group in table.groups]
        for table in built.spec.tables
    }


@settings(max_examples=80, deadline=None)
@given(
    prompts=st.integers(1, 6),
    cohort=st.integers(1, 3),
    pages=st.integers(1, 6),
    responses=st.integers(1, 6),
    factor=st.integers(1, 9),
    grow=st.sets(st.sampled_from(["prompts", "pages", "responses", "contexts"])),
)
def test_only_the_named_tables_grow(
    prompts: int, cohort: int, pages: int, responses: int, factor: int, grow: set[str]
) -> None:
    built = _scenario(prompts, cohort, pages, responses, (1, 1))
    before = _counts(built)
    after = _counts(built.scaled(factor, grow=sorted(grow)))
    assert after["projects"] == before["projects"]  # a named row never grows
    for table in ("prompts", "pages", "responses"):
        times = factor if table in grow else 1
        assert after[table] == [(kind, count * times) for kind, count in before[table]]


@pytest.mark.parametrize(
    ("grow", "contexts"),
    [
        (["responses"], 12),  # children follow their parent: 12 responses, one each
        (["contexts"], 12),  # the parent is fixed, so each of the 3 gets 4
        (["responses", "contexts"], 12),  # not twice: the parent already grew
        (["pages"], 3),
    ],
)
def test_per_parent_children_grow_with_their_parent_or_by_themselves(
    grow: list[str], contexts: int
) -> None:
    built = _scenario(2, 1, 2, 3, (1, 1)).scaled(4, grow=grow)
    assert _sizes(built)["contexts"] == contexts


def test_without_grow_a_sweep_scales_as_before() -> None:
    built = _scenario(2, 1, 2, 3, (1, 2))
    assert built.scaled(4).fingerprint == built.scaled(4, grow=None).fingerprint
    assert built.scaled(4, grow=["pages"]).fingerprint != built.scaled(4).fingerprint


def test_grow_names_tables_the_scenario_has() -> None:
    with pytest.raises(SqlProofScenarioError, match=r"grow names 'page'.*Its tables: contexts"):
        _scenario(2, 1, 2, 3, (1, 1)).scaled(2, grow=["page"])


def test_a_sweep_profile_grows_the_named_tables() -> None:
    built = _scenario(2, 1, 2, 3, (1, 1))
    profile = ScenarioProfile(built, SCHEMA, scale_cohorts=False, grow=["pages"])
    sizes = {name.split(".")[-1]: size for name, size in profile(8).sizes.items()}
    assert sizes == {"projects": 1, "prompts": 3, "pages": 16, "responses": 3, "contexts": 3}


def test_grow_needs_a_scenario() -> None:
    sizes: dict[str, Any] = {"projects": 1}
    with pytest.raises(SqlProofUsageError, match="grow names a scenario's tables"):
        run_sweep(object(), SCHEMA, "fn", sizes=sizes, grow=["projects"])  # type: ignore[arg-type]
