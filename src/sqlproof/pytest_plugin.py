"""Pytest plugin for SqlProof.

Registers the `--sqlproof-*` CLI options and ships ready-to-use
fixtures so tests don't have to define `proof` and `db` themselves.

Fixtures
--------

The plugin provides three fixtures out of the box:

- `sqlproof_database_url` (session) — resolves the target Postgres DSN
  from the CLI flag, then `SQLPROOF_DATABASE_URL`, then `SUPABASE_DB_URL`.
  Skips the test if none are set.
- `proof` (session) — `SqlProof.from_connection_string(...)` bound to
  that DSN, with `disconnect` on teardown.
- `db` (function) — a `SqlProofClient` connected via
  `proof.client_for_dataset({})`, with savepoint isolation per test.

For Supabase projects with `auth.users` FKs, two additional fixtures
opt into auth-user seeding + external-table registration:

- `supabase_proof` (session) — like `proof`, but seeds a deterministic
  pool of `auth.users` rows once and registers `auth.users` as an
  external table so the data generator samples FK targets from that pool.
- `supabase_db` (function) — like `db`, but backed by `supabase_proof`.

Override `proof` (or `supabase_proof`) in your project's conftest.py if
you need different setup; the rest of the chain falls through.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

# Important: don't import sqlproof at module load time. The pytest plugin
# is registered via the `pytest11` entry point and loads BEFORE pytest-cov
# starts measuring. Importing sqlproof here would cause every
# import-time line in the package (class definitions, decorator
# applications, etc.) to be counted as unexecuted by the coverage tool.
# Lazy-import inside each fixture instead.

if TYPE_CHECKING:  # pragma: no cover
    from sqlproof import SqlProof
    from sqlproof.client import SqlProofClient


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register sqlproof's pytest CLI flags.

    Only one flag is exposed: ``--sqlproof-database-url``. The plugin
    previously also declared ``--sqlproof-seed``, ``--sqlproof-runs``,
    ``--sqlproof-show-counterexample``, ``--sqlproof-coverage``,
    ``--sqlproof-diversity-report``, ``--sqlproof-postgres-image``,
    and ``--sqlproof-verbose`` as future-work placeholders that
    were never wired up. They've been removed per #5 / #51's
    deprecation policy — keeping declared-but-no-op flags as a
    "public surface" was misleading users into thinking they
    could control behavior they can't.

    If you actually need any of those behaviors:
      * ``--sqlproof-coverage`` → use ``coverage_session`` from
        ``sqlproof.contrib.plpgsql_coverage`` in your test code
        (the per-session model in plpgsql_check requires the same
        DB connection as your tests, so a CLI flag can't deliver
        it usefully anyway).
      * ``--sqlproof-seed`` / ``--sqlproof-runs`` → configure
        Hypothesis directly via ``@settings(max_examples=N, ...)``
        on your property tests; Hypothesis already exposes the
        seed via ``--hypothesis-seed``.
      * The rest → file an issue describing the actual workflow
        the flag would unlock.
    """
    group = parser.getgroup("sqlproof")
    group.addoption(
        "--sqlproof-database-url",
        action="store",
        help=(
            "Postgres DSN for the `proof` / `db` fixtures. Falls back to "
            "$SQLPROOF_DATABASE_URL, then $SUPABASE_DB_URL."
        ),
    )


# ---------------------------------------------------------------------------
# Database URL resolution
# ---------------------------------------------------------------------------

_DSN_ENV_VARS = ("SQLPROOF_DATABASE_URL", "SUPABASE_DB_URL")


@pytest.fixture(scope="session")
def sqlproof_database_url(request: pytest.FixtureRequest) -> str:
    """Postgres DSN for the SqlProof test session.

    Resolution order:

      1. ``--sqlproof-database-url`` pytest CLI flag.
      2. ``SQLPROOF_DATABASE_URL`` environment variable.
      3. ``SUPABASE_DB_URL`` environment variable (Supabase ergonomics —
         set by ``supabase start`` users by convention).

    If none are set, calls ``pytest.skip`` so DSN-gated suites don't
    fail on developer machines that haven't booted Postgres.
    """
    cli = request.config.getoption("--sqlproof-database-url", default=None)
    if cli:
        return str(cli)
    for env_var in _DSN_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            return value
    pytest.skip(
        "no database URL configured for sqlproof — set "
        f"${_DSN_ENV_VARS[0]} (or ${_DSN_ENV_VARS[1]} on a Supabase "
        "project), or pass --sqlproof-database-url=postgresql://... "
        "on the pytest command line."
    )


# ---------------------------------------------------------------------------
# Core fixtures: proof + db
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def proof(sqlproof_database_url: str) -> Generator[SqlProof]:
    """A `SqlProof` instance bound to the test session's DSN.

    Override in your project's conftest.py if you need to customize the
    construction — typically to register external tables or apply a
    schema override::

        # tests/conftest.py
        import pytest
        from hypothesis import strategies as st
        from sqlproof import ExternalTableSpec, SqlProof

        @pytest.fixture(scope="session")
        def proof(sqlproof_database_url):
            proof = SqlProof.from_connection_string(
                sqlproof_database_url,
                external_tables={
                    "auth.users": ExternalTableSpec(
                        primary_key="id",
                        seed_count=st.integers(min_value=1, max_value=5),
                        sample=lambda db: [
                            row["id"] for row in
                            db.query("SELECT id FROM auth.users LIMIT 5")
                        ],
                    ),
                },
            )
            try:
                yield proof
            finally:
                proof.disconnect()

    For Supabase projects with `auth.users` FKs, the ready-made
    `supabase_proof` fixture handles seeding + external-table
    registration; use that instead.
    """
    from sqlproof import SqlProof  # local import — see top-of-file note

    instance = SqlProof.from_connection_string(sqlproof_database_url)
    try:
        yield instance
    finally:
        instance.disconnect()


@pytest.fixture
def db(proof: SqlProof) -> Generator[SqlProofClient]:
    """A per-test `SqlProofClient` connected via `client_for_dataset({})`.

    Each test gets a fresh savepoint that rolls back on exit, so writes
    don't leak between tests. Use for tests that don't need generated
    data — for property tests with `@given`, take `proof` directly and
    call `proof.client_for_dataset(dataset)` inside the test.
    """
    with proof.client_for_dataset({}) as client:
        yield client


# ---------------------------------------------------------------------------
# Supabase-flavored fixtures: opt in by depending on these instead of `proof`
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def supabase_proof(sqlproof_database_url: str) -> Generator[SqlProof]:
    """A `SqlProof` bound to a Supabase database, pre-seeded for FK draws.

    Differences from the plain `proof` fixture:

      * Calls `seed_test_users_directly(...)` once at session start to
        ensure a deterministic pool of `auth.users` rows exists.
      * Registers `auth.users` as an external table so the data
        generator samples FK targets from the seeded pool, rather than
        trying to invent rows in a Supabase-managed schema it can't
        write to.

    Use this fixture (and the matching `supabase_db`) on Supabase
    projects with RLS / auth.users-keyed RPCs. For non-Supabase
    projects, use the plain `proof` / `db` instead.

    Requires the test connection to have INSERT privilege on
    `auth.users`. Local Supabase grants this; managed Supabase does
    not. For managed environments, run `seed_supabase_test_users` via
    the admin API in your own conftest override.
    """
    # Imports inside the fixture so that projects that never use this
    # path (and don't have `psycopg` or the Supabase contrib in scope)
    # don't pay the import cost at plugin load.
    import psycopg
    from hypothesis import strategies as st
    from psycopg.rows import dict_row

    from sqlproof import ExternalTableSpec, SqlProof
    from sqlproof.client import PsycopgSqlProofClient
    from sqlproof.contrib.supabase import seed_test_users_directly

    seed_count = 5
    with psycopg.connect(
        sqlproof_database_url,
        autocommit=True,
        row_factory=dict_row,  # pyright: ignore[reportArgumentType]
    ) as conn:
        seed_client = PsycopgSqlProofClient(conn)
        seed_test_users_directly(seed_client, count=seed_count)

    def _sample_test_user_ids(db: SqlProofClient) -> list[object]:
        rows = db.query(
            r"""
            SELECT id::text AS id
            FROM auth.users
            WHERE email LIKE %s ESCAPE '\'
            ORDER BY email
            """,
            r"sqlproof\_%@test.invalid",
        )
        return [row["id"] for row in rows]

    instance = SqlProof.from_connection_string(
        sqlproof_database_url,
        external_tables={
            "auth.users": ExternalTableSpec(
                primary_key="id",
                seed_count=st.integers(min_value=1, max_value=seed_count),
                sample=_sample_test_user_ids,
            ),
        },
    )
    try:
        yield instance
    finally:
        instance.disconnect()


@pytest.fixture
def supabase_db(supabase_proof: SqlProof) -> Generator[SqlProofClient]:
    """Per-test `SqlProofClient` backed by `supabase_proof`."""
    with supabase_proof.client_for_dataset({}) as client:
        yield client
