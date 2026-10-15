"""The serialized (YAML / JSON) form of a scenario.

The contract: a Python scenario and its export are the same scenario --
same fingerprint, same rows -- and anything the parser rejects is named
by its path in the file.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import jsonschema
import pytest

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import generate_dataset
from sqlproof.scenarios import DEFAULT_CLOCK, Scenario, ScenarioBuilder, scenario
from sqlproof.scenarios.serialize import dumps, json_schema, load_file
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql

REPO = Path(__file__).resolve().parents[2]

SCHEMA_SQL = """
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text);
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  owner_id uuid REFERENCES auth.users(id),
  name text NOT NULL,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  credit numeric(10, 2),
  joined_on date,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10, 2) NOT NULL CHECK (total >= 0)
);
"""


@pytest.fixture(scope="module")
def schema() -> SchemaInfo:
    return parse_schema_sql(SCHEMA_SQL)


@scenario(seed=42, clock=datetime(2026, 3, 1, tzinfo=UTC))
def tiered(s: ScenarioBuilder) -> None:
    alice = s.row("auth.users", name="alice")
    s.table("auth.users").fill(2)
    s.row("customers", name="acme", tier=1, owner_id=alice, credit=Decimal("12.50"))
    customers = s.table("customers")
    customers.cohort(2, tier=1, joined_on=date(2025, 6, 1)).cohort(3, tier=2).fill(5)
    s.table("invoices").per_parent("customers", where={"tier": 1}, count=(2, 4), default=(0, 1))


def _rows(scenario: Scenario, schema: SchemaInfo) -> dict[str, list[dict[str, Any]]]:
    compiled = scenario.compile(schema)
    return {
        table.qualified_name: list(rows)
        for table, rows in generate_dataset(
            schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns
        )
    }


def _stringify(rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, str]]]:
    """Rows as Postgres would read them: a pinned Decimal and the string
    "12.50" are the same value."""
    return {t: [{k: str(v) for k, v in row.items()} for row in rs] for t, rs in rows.items()}


def test_round_trip_is_the_same_scenario(schema: SchemaInfo) -> None:
    data = tiered.to_dict()
    again = Scenario.from_dict(json.loads(json.dumps(data)))

    assert again.to_dict() == data
    assert again.fingerprint == tiered.fingerprint
    assert again.name == "tiered"
    assert again.clock == tiered.clock
    assert _stringify(_rows(again, schema)) == _stringify(_rows(tiered, schema))
    assert again.compile(schema).refs == tiered.compile(schema).refs


def test_yaml_round_trip(schema: SchemaInfo) -> None:
    text = dumps(tiered, "yaml")
    assert text.startswith("version: 1\nname: tiered\n")
    import yaml

    again = Scenario.from_dict(yaml.safe_load(text))
    assert again.fingerprint == tiered.fingerprint


def test_non_json_pins_are_written_as_postgres_reads_them() -> None:
    @scenario(seed=1)
    def typed(s: ScenarioBuilder) -> None:
        s.table("customers").cohort(
            1,
            credit=Decimal("1.10"),
            joined_on=date(2025, 1, 2),
            owner_id=UUID(int=7),
        )

    pinned = typed.to_dict()["tables"]["customers"][0]["set"]
    assert pinned == {
        "credit": "1.10",
        "joined_on": "2025-01-02",
        "owner_id": "00000000-0000-0000-0000-000000000007",
    }
    assert Scenario.from_dict(typed.to_dict()).fingerprint == typed.fingerprint


def test_values_without_a_serialized_form_are_refused() -> None:
    @scenario(seed=1)
    def odd(s: ScenarioBuilder) -> None:
        s.table("customers").cohort(1, name=b"bytes")

    assert odd.fingerprint
    with pytest.raises(SqlProofScenarioError, match=r"customers\.name is pinned to a bytes"):
        odd.to_dict()


def test_the_documented_example_parses() -> None:
    import yaml

    from sqlproof.scenarios import serialize

    doc = serialize.__doc__ or ""
    start = doc.rindex("\n", 0, doc.index("version: 1")) + 1
    example = doc[start : doc.index("The JSON Schema")]
    parsed = Scenario.from_dict(yaml.safe_load(_dedent(example)))
    assert parsed.name == "tiered_customers"


def _dedent(text: str) -> str:
    lines = text.splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines)


def test_a_named_row_can_pin_a_column_called_name(schema: SchemaInfo) -> None:
    data = {
        "version": 1,
        "name": "named",
        "seed": 3,
        "tables": {"customers": [{"row": "acme", "set": {"name": "Acme Inc", "tier": 2}}]},
    }
    rows = _rows(Scenario.from_dict(data), schema)["public.customers"]
    assert rows[0]["name"] == "Acme Inc"


def test_extensions_are_kept_out_of_the_scenario() -> None:
    data = tiered.to_dict()
    extended = Scenario.from_dict({**data, "x-cloud": {"branch": "preview"}})
    assert extended.fingerprint == tiered.fingerprint


def test_clock_forms() -> None:
    base = {"version": 1, "name": "c", "seed": 1, "tables": {}}
    assert Scenario.from_dict(base).clock == DEFAULT_CLOCK
    naive = Scenario.from_dict({**base, "clock": "2026-05-01T12:00:00"})
    assert naive.clock == datetime(2026, 5, 1, 12, tzinfo=UTC)
    zulu = Scenario.from_dict({**base, "clock": "2026-05-01T12:00:00Z"})
    assert zulu.clock == naive.clock
    assert Scenario.from_dict({**base, "clock": date(2026, 5, 1)}).clock == datetime(
        2026, 5, 1, tzinfo=UTC
    )
    assert Scenario.from_dict({**base, "clock": datetime(2026, 5, 1, 12)}).clock == naive.clock


def test_a_naive_python_clock_is_utc_too() -> None:
    @scenario(seed=1, clock=datetime(2026, 5, 1, 12))
    def naive(s: ScenarioBuilder) -> None:
        pass

    assert naive.clock == datetime(2026, 5, 1, 12, tzinfo=UTC)


def _valid() -> dict[str, Any]:
    return {"version": 1, "name": "v", "seed": 1, "tables": {"customers": [{"fill": 1}]}}


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(colour="red"), r"\$ unknown key\(s\) 'colour'"),
        (lambda d: d.update(version=2), r"\$.version must be 1"),
        (lambda d: d.update(name=""), r"\$.name must be a non-empty string"),
        (lambda d: d.update(seed="42"), r"\$.seed must be an integer"),
        (lambda d: d.update(seed=True), r"\$.seed must be an integer"),
        (lambda d: d.update(clock="soon"), r"\$.clock must be an ISO 8601"),
        (lambda d: d.update(clock=5), r"\$.clock must be an ISO 8601"),
        (lambda d: d.update(tables=[]), r"\$.tables must be a mapping"),
        (lambda d: d["tables"].update(customers={}), r"customers'\] must be a list"),
        (lambda d: d["tables"].update(customers=[3]), r"\[0\] must be a mapping"),
        (lambda d: d["tables"].update(customers=[{}]), r"\[0\] must have exactly one of"),
        (
            lambda d: d["tables"].update(customers=[{"fill": 1, "cohort": 2}]),
            "must have exactly one of",
        ),
        (
            lambda d: d["tables"].update(customers=[{"fill": 1, "set": {}}]),
            "unexpected key\\(s\\) 'set' in a fill group",
        ),
        (lambda d: d["tables"].update(customers=[{"cohort": "2"}]), r"\.cohort must be an int"),
        (lambda d: d["tables"].update(customers=[{"cohort": 0}]), "must be a positive int"),
        (lambda d: d["tables"].update(customers=[{"row": 7}]), r"\.row must be the row's name"),
        (
            lambda d: d["tables"].update(customers=[{"cohort": 1, "set": []}]),
            r"\.set must be a mapping",
        ),
        (
            lambda d: d["tables"].update(customers=[{"cohort": 1, "set": {"tier": [b"1"]}}]),
            r"\.set\.tier\[0\] has unsupported type bytes",
        ),
        (
            lambda d: d["tables"].update(customers=[{"cohort": 1, "set": {"tier": {"x": {1, 2}}}}]),
            r"\.set\.tier\.x has unsupported type set",
        ),
        (
            lambda d: d["tables"].update(customers=[{"row": "a"}, {"row": "a"}]),
            "row name 'a' is used twice",
        ),
        (
            lambda d: d["tables"].update(invoices=[{"per_parent": []}]),
            r"\.per_parent must be a mapping",
        ),
        (
            lambda d: d["tables"].update(invoices=[{"per_parent": {"parent": "customers"}}]),
            "is missing count",
        ),
        (
            lambda d: d["tables"].update(invoices=[{"per_parent": {"count": 1}}]),
            r"\.parent must be a table name",
        ),
        (
            lambda d: d["tables"].update(
                invoices=[{"per_parent": {"parent": "customers", "count": 1, "colour": 1}}]
            ),
            "unexpected key\\(s\\) 'colour'",
        ),
        (
            lambda d: d["tables"].update(
                invoices=[{"per_parent": {"parent": "customers", "count": [1, 2, 3]}}]
            ),
            r"\.count must be an integer or a \[min, max\] pair",
        ),
        (
            lambda d: d["tables"].update(
                invoices=[{"per_parent": {"parent": "customers", "count": [1, "2"]}}]
            ),
            r"\.count\[1\] must be an integer",
        ),
        (
            lambda d: d["tables"].update(
                invoices=[{"per_parent": {"parent": "customers", "count": 1, "column": 3}}]
            ),
            r"\.column must be a column name",
        ),
        (
            lambda d: d["tables"].update(
                invoices=[{"per_parent": {"parent": "customers", "count": [3, 1]}}]
            ),
            "0 <= min <= max",
        ),
    ],
)
def test_invalid_files_name_the_path(change: Any, match: str) -> None:
    data = _valid()
    change(data)
    with pytest.raises(SqlProofScenarioError, match=match):
        Scenario.from_dict(data)


def test_a_non_mapping_is_refused() -> None:
    with pytest.raises(SqlProofScenarioError, match=r"\$ a scenario must be a mapping"):
        Scenario.from_dict([])  # type: ignore[arg-type]


def test_unsupported_scalar_types_are_refused() -> None:
    data = _valid()
    data["tables"]["customers"] = [{"cohort": 1, "set": {"tier": b"1"}}]
    with pytest.raises(SqlProofScenarioError, match="unsupported type bytes"):
        Scenario.from_dict(data)


def test_per_parent_where_forms(schema: SchemaInfo) -> None:
    def per_parent(where: Any) -> dict[str, Any]:
        return {
            "version": 1,
            "name": "pp",
            "seed": 5,
            "tables": {
                "customers": [
                    {"row": "acme", "set": {"tier": 1}},
                    {"cohort": 2, "set": {"tier": 2}},
                ],
                "invoices": [{"per_parent": {"parent": "customers", "where": where, "count": 3}}],
            },
        }

    def children(where: Any) -> int:
        return len(_rows(Scenario.from_dict(per_parent(where)), schema)["public.invoices"])

    assert children({"ref": "acme"}) == 3
    assert children({"tier": 2}) == 6
    assert children(None) == 9


def test_json_schema_accepts_exports_and_rejects_what_the_parser_rejects() -> None:
    validator = jsonschema.Draft202012Validator(json_schema())
    validator.check_schema(json_schema())
    validator.validate(tiered.to_dict())
    validator.validate({**_valid(), "x-anything": [1, 2]})
    # Arrays and json values pin as lists and objects.
    validator.validate(
        {**_valid(), "tables": {"customers": [{"cohort": 1, "set": {"tags": ["a"], "m": {}}}]}}
    )
    for bad in (
        {**_valid(), "colour": "red"},
        {**_valid(), "version": 2},
        {**_valid(), "tables": {"customers": [{"fill": 1, "cohort": 1}]}},
        {**_valid(), "tables": {"customers": [{"cohort": 0, "set": {"tier": 1}}]}},
        {**_valid(), "tables": {"customers": [{"per_parent": {"parent": "x"}}]}},
    ):
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(bad)


def test_the_website_serves_the_packaged_schema() -> None:
    packaged = REPO / "src/sqlproof/schemas/scenario-v1.json"
    served = REPO / "website/public/schemas/scenario-v1.json"
    assert served.read_text(encoding="utf-8") == packaged.read_text(encoding="utf-8")


def test_load_file_json_and_yaml(tmp_path: Path) -> None:
    json_file = tmp_path / "tiered.scenario.json"
    json_file.write_text(dumps(tiered, "json"), encoding="utf-8")
    yaml_file = tmp_path / "tiered.scenario.yaml"
    yaml_file.write_text(dumps(tiered, "yaml"), encoding="utf-8")

    for path in (json_file, yaml_file):
        loaded = load_file(path)
        assert loaded.fingerprint == tiered.fingerprint
        assert loaded.source == str(path)


def test_load_file_errors_name_the_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.scenario.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(SqlProofScenarioError, match=r"broken\.scenario\.json: not valid JSON"):
        load_file(broken)

    bad_yaml = tmp_path / "bad.scenario.yaml"
    bad_yaml.write_text("tables: [", encoding="utf-8")
    with pytest.raises(SqlProofScenarioError, match=r"bad\.scenario\.yaml: not valid YAML"):
        load_file(bad_yaml)

    invalid = tmp_path / "invalid.scenario.json"
    invalid.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(SqlProofScenarioError, match=r"invalid.scenario.json: \$.name"):
        load_file(invalid)

    text = tmp_path / "notes.txt"
    text.write_text("", encoding="utf-8")
    with pytest.raises(SqlProofScenarioError, match=r"end in \.json, \.yaml or \.yml"):
        load_file(text)


def test_yaml_without_pyyaml_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # None in sys.modules makes `import yaml` raise ImportError.
    monkeypatch.setitem(sys.modules, "yaml", None)
    path = tmp_path / "x.scenario.yaml"
    path.write_text("version: 1", encoding="utf-8")
    with pytest.raises(SqlProofScenarioError, match=r"sqlproof\[yaml\]"):
        load_file(path)
    with pytest.raises(SqlProofScenarioError, match=r"sqlproof\[yaml\]"):
        dumps(tiered, "yaml")

    # JSON needs nothing extra.
    json_file = tmp_path / "x.scenario.json"
    json_file.write_text(dumps(tiered, "json"), encoding="utf-8")
    assert load_file(json_file).name == "tiered"
