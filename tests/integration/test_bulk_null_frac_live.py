"""Bulk-path null_frac configurability, verified against pg_stats.

This used to be a *cross-path* test: it asserted that DEFAULT_NULL_FRAC,
tuned to the Hypothesis path's observed null_frac, made bulk-generated
data land at the same pg_stats.null_frac as Hypothesis-generated data.
That premise turned out to be false, and the test was rewritten rather
than re-tuned. Do not reinstate the cross-path comparison without first
re-establishing its premise -- see below.

`st.one_of(st.none(), strategy)` (columns.py) has no stable null rate
to match: the observed rate is an artifact of which SqlProof entry
point draws from the strategy, not a property of the strategy itself.
`core.py`'s `invariant()` draws via `draw_example`, which is
`SearchStrategy.example()` -- internally `@given(strategy)` with
`phases=(Phase.generate,)` and `max_examples=10` only, so it never
leaves Hypothesis's "cold start" region and is heavily biased toward
`one_of`'s first, simplest branch (`none()`). Measured against live
pg_stats (N=800 rows, repeated across 9 independent runs): null_frac
~0.9988-1.0, stable. Drawing the *same* column strategy instead through
a proper `@given(strategy) @settings(max_examples=500)` loop -- many
examples in one continuous search, not a fresh 10-example cold start --
measured at null_frac ~0.002: the opposite extreme, roughly 500x apart,
for an identical strategy. Neither number was chosen by anyone; both
are accidents of how much of the search space got explored before the
strategy was drawn. Matching either one means matching an accident, and
matching the ~1.0 accident specifically would be actively harmful: a
100%-NULL column has degenerate selectivity, the planner treats it as
carrying no information, and any measurement built on data shaped that
way describes nothing real -- the opposite of what this whole
bulk-generation feature exists for.

So `DEFAULT_NULL_FRAC` (bulk.py) is a deliberately chosen, realistic
default rather than a value derived from the Hypothesis path, and it is
a knob every caller can override. What actually needs checking is not
"does bulk match Hypothesis" but "does the knob work": if a caller asks
`load_dataset(..., null_frac=X)`, does a real `COPY` followed by a real
`ANALYZE` leave the planner's own `pg_stats.null_frac` at X? That is
the property later performance measurement actually depends on, and
it's independent of anything the Hypothesis path does. Checked at two
different configured values so one lucky match can't hide a broken
knob -- e.g. a knob wired to nothing would happen to "pass" at
whichever single value equals its hardcoded behavior.

Always asserted via SQL against `pg_stats`, never by reading generated
values back into Python (see test_bulk_copy_live.py's docstring for
why: psycopg deserialises jsonb `null` and real SQL NULL to the same
Python `None`, so a Python-side count can silently agree when the
database disagrees. Not directly applicable to this plain `text`
column, but pg_stats is the actual oracle either way -- it's what the
planner reads, which is the whole point.).
"""

from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE items (
  id bigint PRIMARY KEY,
  label text,
  qty integer NOT NULL
);
"""
SCHEMA_NAME = "bulk_null_frac"
N = 5000

# sampler_for_column draws each nullable value as an independent
# Bernoulli(null_frac) trial (see generators/bulk.py), so at N=5000
# rows the binomial standard deviation of the observed null_frac is
# ~0.0042 at configured=0.1 and ~0.0069 at configured=0.4. A tolerance
# of 0.05 sits more than 7 standard deviations from either configured
# value -- generous enough not to flake on ordinary sampling
# variation, while still catching a genuinely broken knob outright
# (0.0 or 1.0 observed when 0.1 or 0.4 was configured is a >=0.4 gap,
# 8x this tolerance).
TOLERANCE = 0.05


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
        connection.execute(f"CREATE SCHEMA {SCHEMA_NAME}")
        connection.execute(f"SET search_path TO {SCHEMA_NAME}")
        connection.execute(SCHEMA_SQL)
        try:
            yield connection
        finally:
            connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")


@pytest.mark.parametrize("configured_null_frac", [0.1, 0.4])
def test_bulk_null_frac_is_honoured_in_pg_stats(
    conn: psycopg.Connection, configured_null_frac: float
) -> None:
    schema = parse_schema_sql(SCHEMA_SQL, schema=SCHEMA_NAME)
    load_dataset(conn, schema, {"items": N}, seed=1, null_frac=configured_null_frac)
    analyze(conn, schema)

    row = conn.execute(
        "SELECT null_frac FROM pg_stats "
        "WHERE schemaname = %s AND tablename = 'items' AND attname = 'label'",
        (SCHEMA_NAME,),
    ).fetchone()
    assert row is not None, "ANALYZE did not populate pg_stats for items.label"
    observed = row[0]

    assert abs(observed - configured_null_frac) < TOLERANCE, (
        f"configured null_frac={configured_null_frac} but pg_stats reports "
        f"null_frac={observed} for items.label -- the null_frac knob isn't "
        "reaching the database."
    )
