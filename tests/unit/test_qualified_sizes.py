"""`sizes` accepts schema-qualified keys next to bare table names.

Two modelled tables can share a name in different schemas; a bare key
cannot size them separately. The qualified key wins when both are given,
and bare keys keep working unchanged.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from sqlproof.generators.graph import dataset_strategy
from sqlproof.scale.load import missing_required_parents, size_for
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE orgs (id bigint PRIMARY KEY);
CREATE TABLE members (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id)
);
"""


def test_size_for_prefers_the_qualified_key() -> None:
    table = parse_schema_sql(SCHEMA).table("orgs")
    assert size_for({"orgs": 2}, table) == 2
    assert size_for({"public.orgs": 5}, table) == 5
    assert size_for({"orgs": 2, "public.orgs": 5}, table) == 5
    assert size_for({"other.orgs": 9}, table) == 0


def test_missing_required_parents_understands_qualified_keys() -> None:
    schema = parse_schema_sql(SCHEMA)
    assert missing_required_parents(schema, {"public.members": 3, "public.orgs": 1}) == []
    assert missing_required_parents(schema, {"public.members": 3}) == ["members.org_id -> orgs"]


def test_dataset_strategy_reads_qualified_keys() -> None:
    schema = parse_schema_sql(SCHEMA)

    @given(dataset_strategy(schema, sizes={"public.orgs": 2, "members": 3}))
    @settings(max_examples=5, deadline=None, database=None, suppress_health_check=list(HealthCheck))
    def check(dataset: dict[str, list[dict[str, object]]]) -> None:
        assert len(dataset["orgs"]) == 2
        assert len(dataset["members"]) == 3

    check()
