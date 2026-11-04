"""Value generators in scenarios: `format`, `one_of`, `some_of`, `pick`.

A pinned literal gives every row of a group the same value. A value
generator gives each row its own, as a pure function of the scenario,
seed, column and row, so a scenario still loads the same bytes every
time. Generators can sit on a `fill`, the group that grows in a scale
sweep, which is what lets a sweep's data carry relationships the schema
does not declare (a response citing a page's URL).

Tests compile against parsed schemas and run the bulk loader's own
generator (`generate_dataset`), checking the rows a load would send.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import generate_dataset
from sqlproof.scenarios import Scenario, ScenarioBuilder
from sqlproof.scenarios.serialize import dumps, json_schema, scenario_from_dict
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TYPE engine AS ENUM ('chatgpt', 'claude', 'gemini');
CREATE TABLE projects (
  id bigint PRIMARY KEY,
  name text NOT NULL
);
CREATE TABLE brands (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id),
  names text[] NOT NULL,
  label text
);
CREATE TABLE pages (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id),
  url text NOT NULL UNIQUE,
  slug text UNIQUE,
  title text
);
CREATE TABLE responses (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES projects(id),
  engine engine NOT NULL,
  body text NOT NULL,
  score integer,
  kind text,
  first_url text,
  cited text[],
  mentioned text[],
  extra jsonb
);
"""

SCHEMA_INFO = parse_schema_sql(SCHEMA)
TABLES = ("projects", "brands", "pages", "responses")


@pytest.fixture(scope="module")
def schema() -> SchemaInfo:
    return SCHEMA_INFO


def _rows(built: Scenario, schema: SchemaInfo = SCHEMA_INFO) -> dict[str, list[dict[str, Any]]]:
    compiled = built.compile(schema)
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
    for table, rows in generate_dataset(
        schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns
    ):
        out[table.name] = list(rows)
    return out


def _scenario(build: Any, seed: int = 1, name: str = "s") -> Scenario:
    return Scenario(build, seed=seed, name=name)


def _base(s: ScenarioBuilder, *, pages: int = 5, responses: int = 20) -> None:
    acme = s.row("projects", name="acme")
    s.row("brands", name="own", project_id=acme, names=["Acme"])
    s.table("brands").fill(3, project_id=acme, names=[s.format("Rival {n}")])
    s.table("pages").fill(pages, project_id=acme, url=s.format("https://acme.com/p/{n}"))
    s.table("responses").fill(responses, project_id=acme)


# -- format -----------------------------------------------------------------------------


def test_format_numbers_each_row_of_the_table(schema: SchemaInfo) -> None:
    rows = _rows(_scenario(_base), schema)
    assert [r["url"] for r in rows["pages"]] == [f"https://acme.com/p/{n}" for n in range(1, 6)]
    # {n} counts rows of the table, across groups: the named row is 1.
    assert [r["names"] for r in rows["brands"]] == [
        ["Acme"],
        ["Rival 2"],
        ["Rival 3"],
        ["Rival 4"],
    ]


def test_format_writes_text_only(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        s.table("projects").fill(1)
        s.table("responses").fill(2, score=s.format("{n}"))

    with pytest.raises(SqlProofScenarioError, match=r"format writes text.*responses.score"):
        _scenario(build).compile(schema)


# -- one_of, some_of --------------------------------------------------------------------


def test_one_of_draws_each_row_from_its_options(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(40, kind=s.one_of("answer", "refusal"))

    kinds = [r["kind"] for r in _rows(_scenario(build), schema)["responses"][20:]]
    assert set(kinds) == {"answer", "refusal"}


def test_some_of_draws_distinct_values_within_the_count(schema: SchemaInfo) -> None:
    sources = ["https://wiki.org/a", "https://reddit.com/b", "https://news.com/c"]

    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(60, cited=s.some_of(sources, count=(1, 2)))

    for row in _rows(_scenario(build), schema)["responses"][20:]:
        assert 1 <= len(row["cited"]) <= 2
        assert len(set(row["cited"])) == len(row["cited"])
        assert set(row["cited"]) <= set(sources)


def test_some_of_needs_an_array_column(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, kind=s.some_of(["a", "b"], count=1))

    with pytest.raises(SqlProofScenarioError, match=r"some_of writes an array.*responses.kind"):
        _scenario(build).compile(schema)


def test_one_of_checks_its_options_against_the_column(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, engine=s.one_of("chatgpt", "bard"))

    with pytest.raises(SqlProofScenarioError, match="'bard' is not a value of"):
        _scenario(build).compile(schema)


# -- pick -------------------------------------------------------------------------------


def test_pick_takes_values_another_table_generated(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s, pages=8, responses=0)
        s.table("responses").fill(
            50,
            project_id=s.ref("acme"),
            cited=s.pick("pages.url", count=(0, 3)),
            mentioned=s.pick("brands.names", count=(1, 2)),
            first_url=s.pick("pages.url"),
        )

    rows = _rows(_scenario(build), schema)
    urls = {r["url"] for r in rows["pages"]}
    brands = {name for r in rows["brands"] for name in r["names"]}
    for row in rows["responses"]:
        assert set(row["cited"]) <= urls and 0 <= len(row["cited"]) <= 3
        assert set(row["mentioned"]) <= brands and 1 <= len(row["mentioned"]) <= 2
        assert row["first_url"] in urls
    assert any(row["cited"] for row in rows["responses"])


def test_pick_from_a_generated_unique_column(schema: SchemaInfo) -> None:
    """A UNIQUE column the generator fills is a function of the row, so it
    can be picked from without storing it."""

    def build(s: ScenarioBuilder) -> None:
        _base(s, responses=0)
        s.table("responses").fill(10, project_id=s.ref("acme"), first_url=s.pick("pages.slug"))

    rows = _rows(_scenario(build), schema)
    slugs = {r["slug"] for r in rows["pages"]}
    assert {r["first_url"] for r in rows["responses"]} <= slugs


def test_pick_refuses_a_random_column(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, first_url=s.pick("pages.title"))

    with pytest.raises(SqlProofScenarioError, match=r"pages\.title is generated at random"):
        _scenario(build).compile(schema)


def test_pick_from_an_empty_table_gives_empty_arrays(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s, pages=0)
        s.table("responses").fill(5, cited=s.pick("pages.url", count=(1, 3)))

    assert all(r["cited"] == [] for r in _rows(_scenario(build), schema)["responses"][20:])


def test_pick_into_a_not_null_column_needs_source_rows(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s, pages=0)
        s.table("responses").fill(5, body=s.pick("pages.url"))

    with pytest.raises(
        SqlProofScenarioError, match=r"responses\.body is NOT NULL.*pages has no rows"
    ):
        _scenario(build).compile(schema)


def test_pick_count_is_for_array_columns(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, first_url=s.pick("pages.url", count=(1, 2)))

    with pytest.raises(SqlProofScenarioError, match=r"responses\.first_url is not an array"):
        _scenario(build).compile(schema)


@pytest.mark.parametrize("source", ["pages", "pages.nope", "nope.url"])
def test_pick_names_an_existing_column(schema: SchemaInfo, source: str) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, first_url=s.pick(source))

    with pytest.raises(SqlProofScenarioError, match="pick"):
        _scenario(build).compile(schema)


# -- where generators cannot go ------------------------------------------------------


def test_generators_stay_off_foreign_keys(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        s.table("projects").fill(2)
        s.table("pages").fill(2, project_id=s.one_of(1, 2), url=s.format("u{n}"))

    with pytest.raises(SqlProofScenarioError, match=r"pages\.project_id is a foreign key"):
        _scenario(build).compile(schema)


def test_only_format_can_fill_a_unique_column(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("pages").fill(3, project_id=s.ref("acme"), url=s.one_of("a", "b"))

    with pytest.raises(SqlProofScenarioError, match=r"pages\.url is UNIQUE"):
        _scenario(build).compile(schema)


def test_a_generator_on_a_json_column_must_be_meant(schema: SchemaInfo) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s)
        s.table("responses").fill(2, extra=s.one_of("a", "b"))

    with pytest.raises(SqlProofScenarioError, match=r"responses\.extra is json.*\{json: \.\.\.\}"):
        _scenario(build).compile(schema)


# -- the serialized form ----------------------------------------------------------------

YAML_SCENARIO = {
    "version": 1,
    "name": "visibility",
    "seed": 3,
    "tables": {
        "projects": [{"row": "acme"}],
        "brands": [
            {"row": "own", "set": {"project_id": {"ref": "acme"}, "names": ["Acme"]}},
            {"fill": 3, "set": {"project_id": {"ref": "acme"}, "names": [{"format": "Rival {n}"}]}},
        ],
        "pages": [
            {"fill": 4, "set": {"project_id": {"ref": "acme"}, "url": {"format": "https://a.com/{n}"}}}
        ],
        "responses": [
            {
                "fill": 30,
                "set": {
                    "project_id": {"ref": "acme"},
                    "kind": {"one_of": ["answer", "refusal"]},
                    "cited": {"pick": "pages.url", "count": [0, 3]},
                    "mentioned": {"some_of": ["Acme", "Other"], "count": [0, 2]},
                    "extra": {"json": {"format": "kept as data"}},
                },
            }
        ],
    },
}


def test_the_yaml_form_loads_and_round_trips(schema: SchemaInfo) -> None:
    jsonschema.Draft202012Validator(json_schema()).validate(YAML_SCENARIO)
    built = scenario_from_dict(YAML_SCENARIO)
    again = scenario_from_dict(built.to_dict())
    assert again.fingerprint == built.fingerprint
    assert _rows(again, schema) == _rows(built, schema)
    rows = _rows(built, schema)["responses"]
    assert {r["kind"] for r in rows} <= {"answer", "refusal"}
    assert all(r["extra"] == {"format": "kept as data"} for r in rows)


def test_a_json_literal_that_looks_like_a_generator_is_written_wrapped() -> None:
    built = scenario_from_dict(YAML_SCENARIO)
    data = built.to_dict()
    assert data["tables"]["responses"][0]["set"]["extra"] == {"json": {"format": "kept as data"}}
    assert "format: Rival {n}" in dumps(built)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"format": 3}, "format must be a string"),
        ({"one_of": []}, "one_of needs at least one value"),
        ({"some_of": ["a"], "count": [2, 1]}, "count"),
        ({"pick": "pages"}, "pick names a column as table.column"),
        ({"pick": "pages.url", "count": "many"}, "count"),
        ({"json": 1, "extra": 2}, "unexpected key"),
    ],
)
def test_bad_generators_are_refused_with_their_path(value: dict[str, Any], problem: str) -> None:
    data = {
        "version": 1,
        "name": "bad",
        "seed": 1,
        "tables": {"responses": [{"fill": 1, "set": {"kind": value}}]},
    }
    with pytest.raises(SqlProofScenarioError, match=r"\$\.tables\['responses'\]\[0\]\.set\.kind"):
        scenario_from_dict(data)
    with pytest.raises(SqlProofScenarioError, match=problem):
        scenario_from_dict(data)


def test_a_generator_changes_the_fingerprint() -> None:
    def with_kind(options: tuple[str, ...]) -> Scenario:
        return _scenario(lambda s: s.table("responses").fill(1, kind=s.one_of(*options)))

    assert with_kind(("a", "b")).fingerprint != with_kind(("a", "c")).fingerprint
    assert with_kind(("a", "b")).fingerprint == with_kind(("a", "b")).fingerprint


# -- properties -------------------------------------------------------------------------

HINTS = ("kind_one_of", "body_format", "cited_some_of", "cited_pick", "score_one_of")


def _hinted(pages: int, responses: int, hint: str | None) -> Any:
    def build(s: ScenarioBuilder) -> None:
        _base(s, pages=pages, responses=0)
        extra: dict[str, Any] = {}
        if hint == "kind_one_of":
            extra["kind"] = s.one_of("a", "b", "c")
        elif hint == "body_format":
            extra["body"] = s.format("answer {n}")
        elif hint == "cited_some_of":
            extra["cited"] = s.some_of(["x", "y", "z"], count=(0, 2))
        elif hint == "cited_pick":
            extra["cited"] = s.pick("pages.url", count=(0, 3))
        elif hint == "score_one_of":
            extra["score"] = s.one_of(1, 2, None)
        s.table("responses").fill(responses, project_id=s.ref("acme"), **extra)

    return build


HINTED_COLUMN = {
    "kind_one_of": "kind",
    "body_format": "body",
    "cited_some_of": "cited",
    "cited_pick": "cited",
    "score_one_of": "score",
}


@settings(max_examples=60, deadline=None)
@given(
    pages=st.integers(0, 6),
    responses=st.integers(1, 12),
    hint=st.sampled_from(HINTS),
    seed=st.integers(-(2**31), 2**31),
)
def test_a_generator_moves_only_its_own_column(
    pages: int, responses: int, hint: str, seed: int
) -> None:
    """Adding a generator to a column leaves every other column of every
    table as it was: the column still takes the random draws it would
    have taken, so nothing generated after it shifts."""
    plain = _rows(_scenario(_hinted(pages, responses, None), seed=seed))
    hinted = _rows(_scenario(_hinted(pages, responses, hint), seed=seed))
    column = HINTED_COLUMN[hint]
    for table in TABLES:
        assert len(plain[table]) == len(hinted[table])
        for before, after in zip(plain[table], hinted[table], strict=True):
            names = before.keys() | after.keys()
            changed = {name for name in names if before.get(name) != after.get(name)}
            assert changed <= ({column} if table == "responses" else set())


@settings(max_examples=60, deadline=None)
@given(
    pages=st.integers(0, 6),
    responses=st.integers(0, 12),
    hint=st.sampled_from(HINTS),
    seed=st.integers(-(2**31), 2**31),
)
def test_a_scenario_with_generators_loads_the_same_rows_every_time(
    pages: int, responses: int, hint: str, seed: int
) -> None:
    build = _hinted(pages, responses, hint)
    assert _rows(_scenario(build, seed=seed)) == _rows(_scenario(build, seed=seed))


@settings(max_examples=60, deadline=None)
@given(
    pages=st.integers(0, 8),
    responses=st.integers(0, 15),
    low=st.integers(0, 4),
    width=st.integers(0, 4),
    factor=st.sampled_from([1, 2, 3, 8]),
    seed=st.integers(-(2**31), 2**31),
)
def test_every_pick_lands_on_a_value_the_source_generated(
    pages: int, responses: int, low: int, width: int, factor: int, seed: int
) -> None:
    def build(s: ScenarioBuilder) -> None:
        _base(s, pages=pages, responses=0)
        s.table("responses").fill(
            responses, project_id=s.ref("acme"), cited=s.pick("pages.url", count=(low, low + width))
        )

    rows = _rows(_scenario(build, seed=seed).scaled(factor))
    urls = {r["url"] for r in rows["pages"]}
    assert len(urls) == len(rows["pages"])  # format keeps a UNIQUE column unique
    for row in rows["responses"]:
        picked = row["cited"]
        assert set(picked) <= urls
        assert len(picked) == len(set(picked))
        assert min(low, len(urls)) <= len(picked) <= min(low + width, len(urls))


@settings(max_examples=40, deadline=None)
@given(
    pages=st.integers(1, 6),
    responses=st.integers(1, 10),
    hint=st.sampled_from(HINTS),
)
def test_minimal_and_scaled_variants_keep_their_generators(
    pages: int, responses: int, hint: str
) -> None:
    built = _scenario(_hinted(pages, responses, hint))
    for variant in (built.minimal(), built.scaled(3)):
        rows = _rows(variant)
        column = HINTED_COLUMN[hint]
        if hint == "body_format":
            assert all(r["body"].startswith("answer ") for r in rows["responses"])
        if hint == "kind_one_of":
            assert {r[column] for r in rows["responses"]} <= {"a", "b", "c"}


@settings(max_examples=40, deadline=None)
@given(
    pages=st.integers(0, 5),
    responses=st.integers(0, 8),
    hint=st.sampled_from(HINTS),
    seed=st.integers(0, 2**31),
)
def test_the_serialized_form_round_trips(pages: int, responses: int, hint: str, seed: int) -> None:
    built = _scenario(_hinted(pages, responses, hint), seed=seed, name="round")
    jsonschema.Draft202012Validator(json_schema()).validate(built.to_dict())
    again = scenario_from_dict(built.to_dict())
    assert again.fingerprint == built.fingerprint
    assert _rows(again) == _rows(built)


# -- every refusal names its column -----------------------------------------------------

EDGE_SCHEMA = parse_schema_sql(
    """
    CREATE TABLE owners (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id uuid REFERENCES auth.users(id),
      handle text,
      ref_code uuid NOT NULL DEFAULT gen_random_uuid(),
      scores integer[]
    );
    CREATE TABLE notes (
      id bigint PRIMARY KEY,
      owner_id uuid REFERENCES owners(id),
      label text,
      refs text[],
      codes text[]
    );
    """
)


def _edge(build: Any) -> Scenario:
    return Scenario(build, seed=2, name="edge")


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        (
            lambda s: s.table("notes").fill(1, label=[s.format("a{n}")]),
            r"notes\.label is not an array; pin one value",
        ),
        (
            lambda s: s.table("owners").fill(1, scores=[s.format("{n}")]),
            r"format writes text; the elements of public\.owners\.scores are",
        ),
        (
            lambda s: s.table("notes").fill(1, refs=[s.one_of("a", "b")]),
            "inside an array, only format is supported",
        ),
        (
            lambda s: (
                s.table("auth.users").fill(1),
                s.table("notes").fill(1, label=s.pick("auth.users.id")),
            ),
            "pick cannot read auth.users",
        ),
        (
            lambda s: (
                s.table("owners").fill(1, handle=s.pick("notes.label")),
                s.table("notes").fill(
                    1, label=s.format("l{n}"), refs=s.pick("owners.handle", count=1)
                ),
            ),
            r"owners\.handle is itself picked",
        ),
        (
            lambda s: (
                s.table("owners").cohort(1, handle=lambda ctx: "x"),
                s.table("notes").fill(1, label=s.pick("owners.handle")),
            ),
            r"owners\.handle is pinned to a callable",
        ),
        (
            lambda s: (
                s.table("owners").fill(2),
                s.table("notes").fill(1, label=s.pick("owners.handle")),
            ),
            r"owners\.handle is generated at random on a fill",
        ),
    ],
)
def test_each_misuse_is_refused_with_its_column(build: Any, problem: str) -> None:
    with pytest.raises(SqlProofScenarioError, match=problem):
        _edge(build).compile(EDGE_SCHEMA)


def test_pick_reads_keys_and_volatile_defaults() -> None:
    """A uuid key or a gen_random_uuid() default is derived from the seed
    and row, so it can be picked from."""

    def build(s: ScenarioBuilder) -> None:
        s.table("owners").fill(3)
        s.table("notes").fill(6, label=s.pick("owners.ref_code"), refs=s.pick("owners.id"))

    rows = _rows(_edge(build), EDGE_SCHEMA)
    codes = {r["ref_code"] for r in rows["owners"]}
    ids = {r["id"] for r in rows["owners"]}
    for row in rows["notes"]:
        assert row["label"] in codes
        assert len(row["refs"]) == 1 and row["refs"][0] in ids  # an array pick without count: one


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        (lambda s: s.format(3), "format must be a string"),
        (lambda s: s.one_of(), "one_of needs at least one value"),
        (lambda s: s.some_of("abc", count=1), "some_of needs a non-empty list"),
        (lambda s: s.some_of([], count=1), "some_of needs a non-empty list"),
        (lambda s: s.some_of(["a"], count=True), "count must be an int"),
        (lambda s: s.some_of(["a"], count=(1, "2")), "count must be an int"),
        (lambda s: s.some_of(["a"], count=[1, 2, 3]), "count must be an int"),
        (lambda s: s.pick("url"), "pick names a column as table.column"),
        (lambda s: s.pick("pages."), "pick names a column as table.column"),
    ],
)
def test_the_python_builders_check_their_arguments(make: Any, problem: str) -> None:
    with pytest.raises(SqlProofScenarioError, match=problem):
        make(ScenarioBuilder())


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"format": "a", "one_of": ["b"]}, "mixes format and one_of"),
        ({"some_of": ["a"]}, "some_of needs a count"),
        ({"some_of": "a", "count": 1}, "some_of needs a non-empty list"),
        ({"one_of": "a"}, "one_of needs at least one value"),
        ({"pick": 3}, "pick names a column as table.column"),
    ],
)
def test_more_bad_generators_in_files(value: dict[str, Any], problem: str) -> None:
    data = {
        "version": 1,
        "name": "bad",
        "seed": 1,
        "tables": {"notes": [{"fill": 1, "set": {"refs": [value]}}]},
    }
    with pytest.raises(SqlProofScenarioError, match=problem):
        scenario_from_dict(data)
