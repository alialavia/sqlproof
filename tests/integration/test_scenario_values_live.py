"""Value generators and per-table growth against a real database.

Postgres is the judge: scenarios load into a schema with foreign keys, a
UNIQUE column, CHECKs and a foreign-key cycle, so an invalid row fails
the load. Then two acceptance tests reproduce the kind of incident these
features exist for -- a function whose cost depends on a dimension the
default sweep keeps fixed, and one whose cost depends on rows actually
matching -- and check that a sweep sees it only with them.
"""

from __future__ import annotations

import math
import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.config import SqlProofConfig
from sqlproof.scale import scale_analysis
from sqlproof.scenarios import RowRef, Scenario, ScenarioBuilder

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"
SCHEMA = "values_live"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

PAD = "x" * 1500  # stored inline: about five rows per page, so scans cost pages read

SCHEMA_SQL = f"""
CREATE TYPE {SCHEMA}.engine AS ENUM ('chatgpt', 'claude', 'gemini');
CREATE TABLE {SCHEMA}.projects (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE {SCHEMA}.brands (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES {SCHEMA}.projects(id),
  names text[] NOT NULL CHECK (cardinality(names) > 0)
);
CREATE TABLE {SCHEMA}.pages (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES {SCHEMA}.projects(id),
  url text NOT NULL UNIQUE,
  pad text
);
CREATE TABLE {SCHEMA}.scans (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES {SCHEMA}.projects(id),
  first_response_id bigint
);
CREATE TABLE {SCHEMA}.responses (
  id bigint PRIMARY KEY,
  project_id bigint NOT NULL REFERENCES {SCHEMA}.projects(id),
  scan_id bigint REFERENCES {SCHEMA}.scans(id),
  engine {SCHEMA}.engine NOT NULL,
  score integer CHECK (score BETWEEN 0 AND 100),
  cited text[],
  mentioned text[],
  pad text
);
ALTER TABLE {SCHEMA}.scans ADD CONSTRAINT scans_first_response
  FOREIGN KEY (first_response_id) REFERENCES {SCHEMA}.responses(id);

-- Rescans every page per response: lower(url) cannot use the url index.
CREATE FUNCTION {SCHEMA}.cited_pages_rescan(p bigint) RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE r record; n bigint := 0; k bigint;
BEGIN
  FOR r IN SELECT cited FROM {SCHEMA}.responses WHERE project_id = p LOOP
    SELECT count(*) INTO k FROM {SCHEMA}.pages
     WHERE project_id = p AND lower(url) = ANY(r.cited);
    n := n + k;
  END LOOP;
  RETURN n;
END $$;

-- The fix: the same answer through the url index.
CREATE FUNCTION {SCHEMA}.cited_pages_indexed(p bigint) RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE r record; n bigint := 0; k bigint;
BEGIN
  FOR r IN SELECT cited FROM {SCHEMA}.responses WHERE project_id = p LOOP
    SELECT count(*) INTO k FROM {SCHEMA}.pages WHERE url = ANY(r.cited);
    n := n + k;
  END LOOP;
  RETURN n;
END $$;

-- Per citation that matches a page, a scan of every response: the cost
-- grows with matches, so random citations never show it.
CREATE FUNCTION {SCHEMA}.page_citation_counts(p bigint) RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE r record; u text; n bigint := 0; k bigint;
BEGIN
  FOR r IN SELECT cited FROM {SCHEMA}.responses WHERE project_id = p LOOP
    FOREACH u IN ARRAY coalesce(r.cited, '{{}}') LOOP
      IF EXISTS (SELECT 1 FROM {SCHEMA}.pages WHERE url = u) THEN
        SELECT count(*) INTO k FROM {SCHEMA}.responses r2 WHERE u = ANY(r2.cited);
        n := n + k;
      END IF;
    END LOOP;
  END LOOP;
  RETURN n;
END $$;
"""


@pytest.fixture
def proof():
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        try:
            connection.execute(f"CREATE SCHEMA {SCHEMA}")
            connection.execute(SCHEMA_SQL)
            yield SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=SCHEMA))
        finally:
            connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _visibility(
    *, pages: int = 10, responses: int = 20, seed: int = 9, cite: str = "pick"
) -> Scenario:
    def build(s: ScenarioBuilder) -> None:
        acme = s.row("projects", name="acme")
        s.row("brands", name="own", project_id=acme, names=["Acme"])
        s.table("brands").fill(3, project_id=acme, names=[s.format("Rival {n}")])
        s.table("pages").fill(
            pages, project_id=acme, url=s.format("https://acme.com/blog/{n}"), pad=PAD
        )
        s.table("scans").fill(4, project_id=acme)
        cited = (
            s.pick("pages.url", count=(1, 3))
            if cite == "pick"
            else s.some_of(["https://wiki.org/a", "https://news.com/b"], count=(1, 2))
        )
        s.table("responses").fill(
            responses,
            project_id=acme,
            engine=s.one_of("chatgpt", "claude"),
            cited=cited,
            mentioned=s.pick("brands.names", count=(0, 2)),
            pad=PAD,
        )

    return Scenario(build, seed=seed, name="visibility")


def _invariants(dsn: str) -> tuple[int, int, int]:
    """Rows citing a URL no page has, mentioning an untracked brand, and
    responses on an engine outside one_of's options."""
    with database.connect(dsn, autocommit=True) as conn:
        dangling = conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.responses r, unnest(r.cited) u "
            f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.pages p WHERE p.url = u)"
        ).fetchone()[0]
        untracked = conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.responses r, unnest(r.mentioned) m "
            f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.brands b WHERE m = ANY(b.names))"
        ).fetchone()[0]
        engines = conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.responses WHERE engine NOT IN ('chatgpt', 'claude')"
        ).fetchone()[0]
    return dangling, untracked, engines


def test_a_scenario_with_generators_loads_with_every_constraint_on(proof) -> None:
    dsn = os.environ[DSN_ENV]
    report = _visibility().load(dsn, schema=SCHEMA)
    assert report.rows[f"{SCHEMA}.responses"] == 20
    assert _invariants(dsn) == (0, 0, 0)
    with database.connect(dsn, autocommit=True) as conn:
        # The cycle's deferred edge (scans -> responses) still loads, and
        # every response cites at least one page.
        assert conn.execute(
            f"SELECT count(*) FROM {SCHEMA}.responses WHERE cardinality(cited) = 0"
        ).fetchone()[0] == 0


def test_a_reload_gives_the_same_rows(proof) -> None:
    dsn = os.environ[DSN_ENV]
    query = f"SELECT id, engine, cited, mentioned FROM {SCHEMA}.responses ORDER BY id"
    _visibility().load(dsn, schema=SCHEMA)
    with database.connect(dsn, autocommit=True) as conn:
        first = conn.execute(query).fetchall()
    _visibility().load(dsn, schema=SCHEMA, truncate_existing=True)
    with database.connect(dsn, autocommit=True) as conn:
        assert conn.execute(query).fetchall() == first


def test_the_calibration_scenario_loads_with_empty_picks(proof) -> None:
    dsn = os.environ[DSN_ENV]
    _visibility(pages=0).minimal().load(dsn, schema=SCHEMA)
    with database.connect(dsn, autocommit=True) as conn:
        assert conn.execute(f"SELECT cited FROM {SCHEMA}.responses").fetchall() == [([],)]


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    pages=st.integers(0, 12),
    responses=st.integers(0, 30),
    seed=st.integers(0, 2**31),
    factor=st.sampled_from([1, 2, 5]),
)
def test_any_generated_scenario_satisfies_postgres(proof, pages, responses, seed, factor) -> None:
    dsn = os.environ[DSN_ENV]
    built = _visibility(pages=max(pages, 1), responses=responses, seed=seed).scaled(
        factor, grow=["pages", "responses"]
    )
    built.load(dsn, schema=SCHEMA, truncate_existing=True)
    assert _invariants(dsn) == (0, 0, 0)


# -- acceptance --------------------------------------------------------------------------


def _growth(proof, function: str, built: Scenario, grow: list[str] | None, tmp_path) -> float:
    """How the function's work grows from 1x to 16x, as an exponent read
    from the end points: about 1 when it doubles with the data, about 2
    when it quadruples. Read from the measured points rather than
    `ScaleResult.exponent`, whose fit refuses a curve as flat and noisy as
    the controls' (a few index probes per row over a fixed per-call cost).
    """
    result = scale_analysis(
        proof,
        f"{SCHEMA}.{function}",
        scenario=built,
        args=[RowRef("acme")],
        grow=grow,
        max_factor=16,
        min_points=5,
        truncate_existing=True,
        artifact_dir=tmp_path,
    )
    first, last = result.points[0], result.points[-1]
    assert (first.factor, last.factor) == (1, 16)
    work = [max(point.work_blocks - result.baseline, 1) for point in (first, last)]
    return math.log(work[1] / work[0]) / math.log(16)


def test_a_sweep_sees_a_page_rescan_only_when_pages_grow(proof, tmp_path) -> None:
    """Responses x pages: quadratic when both grow. Growing responses
    alone, as a sweep did before `grow`, reads as linear and misses it."""
    built = _visibility()
    both = ["pages", "responses"]
    assert _growth(proof, "cited_pages_rescan", built, both, tmp_path) > 1.6
    assert _growth(proof, "cited_pages_rescan", built, ["responses"], tmp_path) < 1.3
    assert _growth(proof, "cited_pages_indexed", built, both, tmp_path) < 1.3


def test_a_sweep_sees_per_match_work_only_when_citations_match(proof, tmp_path) -> None:
    """Work per citation that hits a page: quadratic with pick, invisible
    with random citations that never hit."""
    grow = ["pages", "responses"]
    assert _growth(proof, "page_citation_counts", _visibility(), grow, tmp_path) > 1.6
    unmatched = _visibility(cite="random")
    assert _growth(proof, "page_citation_counts", unmatched, grow, tmp_path) < 1.3
