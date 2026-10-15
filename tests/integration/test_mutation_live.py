"""End-to-end mutation run against live Postgres.

Builds a throwaway template database (table + LANGUAGE sql function),
writes a minimal pytest suite to tmp_path, and runs two mutants:

  - WHERE-clause inversion -> the suite recomputes the sum in Python,
    so this mutant MUST be killed.
  - COALESCE fallback 0 -> 1 -> the suite never queries a user with
    zero rows, so this mutant MUST survive (the classic untested
    empty-group case).

Skips if SQLPROOF_TEST_DATABASE_URL is unset. Requires CREATEDB rights
on the target server (the CI supabase/postgres service has them).
"""

from __future__ import annotations

import os
import re
import textwrap
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

from sqlproof import _database as database
from sqlproof import _dsn as conninfo
from sqlproof._dsn import quote_identifier
from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation import runner as runner_module
from sqlproof.mutation.model import MutationSet, Replace
from sqlproof.mutation.result import MutationResult
from sqlproof.mutation.runner import LocalMutationRunner, run_mutation_tests

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL CHECK (amount >= 0)
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;
"""

INNER_TEST = """
    from __future__ import annotations

    import os

    from sqlproof import _database as database


    def test_total_usage_matches_python_sum() -> None:
        dsn = os.environ["SQLPROOF_MUTATION_TEST_DSN"]
        with database.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM usage_events")
            rows = [(1, 10), (1, 32), (2, 5)]
            for user_id, amount in rows:
                connection.execute(
                    "INSERT INTO usage_events (user_id, amount) VALUES (%s, %s)",
                    (user_id, amount),
                )
            cursor = connection.execute("SELECT total_usage(1)")
            assert cursor.fetchone()[0] == 42
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_mutation_run_kills_and_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    # The run's id, known here so the leftover check below sees only this
    # run's clones: mutant ids are content hashes, the same in every run of
    # this test, so another run in flight on the same server (CI shards,
    # a developer's parallel loop) has clones matching the mutant ids too.
    run_id = uuid4().hex[:16]
    monkeypatch.setattr(runner_module, "new_run_id", lambda: run_id)
    template_name = f"sqlproof_mut_tmpl_{uuid4().hex[:12]}"

    with database.connect(base_dsn, autocommit=True) as admin:
        admin.execute(
            f"CREATE DATABASE {quote_identifier(template_name)}"
        )
    try:
        parts = conninfo.conninfo_to_dict(base_dsn)
        parts["dbname"] = template_name
        template_dsn = conninfo.make_conninfo(**parts)
        with database.connect(template_dsn, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        # The template must have zero connections during cloning -> the
        # context managers above are closed before run_mutation_tests.

        schema_file = tmp_path / "schema.sql"
        schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
        test_file = tmp_path / "test_inner_billing.py"
        test_file.write_text(textwrap.dedent(INNER_TEST), encoding="utf-8")

        mutations = MutationSet.for_function(
            "total_usage",
            [
                Replace("user_id = p_user", "user_id <> p_user"),
                Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
            ],
        )
        result = run_mutation_tests(
            mutations,
            schema_file=schema_file,
            database_url=template_dsn,
            pytest_args=[str(test_file), "-q", "-p", "no:cacheprovider"],
            env_var="SQLPROOF_MUTATION_TEST_DSN",
            # A pooler (Supavisor) keeps an idle session open on the
            # template after the setup connection above closes. Directly
            # connected there are no other sessions and this is a no-op.
            release_template_sessions=True,
        )

        statuses = {o.description: o.status for o in result.outcomes}
        assert statuses[
            "total_usage: replace 'user_id = p_user' -> 'user_id <> p_user'"
        ] == "killed", result.to_dict()
        assert statuses[
            "total_usage: replace 'COALESCE(SUM(amount), 0)' -> 'COALESCE(SUM(amount), 1)'"
        ] == "survived", result.to_dict()

        with pytest.raises(SqlProofMutationError, match="COALESCE"):
            result.assert_no_survivors()

        # No clone databases left behind by THIS run (scoped to its run id
        # -- a shared server may hold orphans from interrupted runs, or
        # clones of a concurrent run, which are not this test's concern).
        # Clone names end in the run id (the stem may be cut to fit).
        clone_patterns = [f"sqlproof\\_%\\_{run_id}"]
        with database.connect(base_dsn, autocommit=True) as admin:
            cursor = admin.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE ANY(%s)",
                (clone_patterns,),
            )
            assert cursor.fetchall() == []
    finally:
        with database.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                f"DROP DATABASE IF EXISTS {quote_identifier(template_name)} WITH (FORCE)"
            )


@pytest.fixture
def idle_template(tmp_path: Path):
    """A template database with one idle session held open on it, the
    way a connection pooler keeps one after its client disconnects."""
    base_dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    name = f"sqlproof_mut_idle_{uuid4().hex[:12]}"
    with database.connect(base_dsn, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {quote_identifier(name)}")
    dsn = conninfo.make_conninfo(base_dsn, dbname=name, application_name="fake-pooler")
    holder = database.connect(dsn, autocommit=True)
    # The name the *server* sees: a pooler replaces it with its own.
    seen_as = holder.execute("SHOW application_name").fetchone()[0]
    try:
        yield dsn, seen_as
    finally:
        with suppress(Exception):
            holder.close()
        with database.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE)")


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_an_idle_session_on_the_template_is_named_in_the_error(idle_template) -> None:
    dsn, seen_as = idle_template
    runner = LocalMutationRunner(database_url=dsn, pytest_args=[])
    with pytest.raises(
        SqlProofMutationError, match=rf"{re.escape(seen_as)}.*release_template_sessions"
    ):
        runner.run_baseline()


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_release_template_sessions_clones_past_an_idle_session(idle_template) -> None:
    dsn, _seen_as = idle_template
    runner = LocalMutationRunner(
        database_url=dsn, pytest_args=[], release_template_sessions=True
    )
    clone_name = runner._clone_name("sqlproof_baseline")
    clone_dsn = runner._create_clone(clone_name)
    try:
        with database.connect(clone_dsn, autocommit=True) as clone:
            assert clone.execute("SELECT 1").fetchone() == (1,)
    finally:
        runner._drop_clone(clone_name)
    # The template is open to connections again afterwards.
    with database.connect(dsn, autocommit=True) as template:
        assert template.execute(
            "SELECT datallowconn FROM pg_database WHERE datname = current_database()"
        ).fetchone() == (True,)


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_release_leaves_an_active_session_alone(idle_template) -> None:
    dsn, _seen_as = idle_template
    with database.connect(dsn, autocommit=True) as busy:
        busy_pid = busy.execute("SELECT pg_backend_pid()").fetchone()[0]
        busy.execute("BEGIN")
        busy.execute("SELECT 1")  # idle in transaction: not plain idle
        runner = LocalMutationRunner(
            database_url=dsn, pytest_args=[], release_template_sessions=True
        )
        with pytest.raises(SqlProofMutationError, match="left alone"):
            runner._create_clone(runner._clone_name("sqlproof_baseline"))
        assert busy.execute("SELECT pg_backend_pid()").fetchone()[0] == busy_pid
        busy.execute("ROLLBACK")


SLOW_INNER_TEST = """
    from __future__ import annotations

    import os
    import time

    from sqlproof import _database as database


    def test_total_usage_of_an_unknown_user_is_zero() -> None:
        dsn = os.environ["SQLPROOF_MUTATION_TEST_DSN"]
        # Hold the clone long enough that the other run's clones are
        # created and dropped while this one is in use.
        time.sleep(1.0)
        with database.connect(dsn, autocommit=True) as connection:
            assert connection.execute("SELECT total_usage(-1)").fetchone()[0] == 0
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_concurrent_runs_on_one_cluster_do_not_drop_each_others_clones(
    tmp_path: Path,
) -> None:
    # Two developers (or CI jobs) mutating the same function on one server:
    # identical mutant ids, separate templates, overlapping runs.
    base_dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    templates = [f"sqlproof_mut_conc_{uuid4().hex[:12]}" for _ in range(2)]
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    test_file = tmp_path / "test_inner_slow.py"
    test_file.write_text(textwrap.dedent(SLOW_INNER_TEST), encoding="utf-8")
    mutations = MutationSet.for_function(
        "total_usage", [Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)")]
    )

    with database.connect(base_dsn, autocommit=True) as admin:
        for name in templates:
            admin.execute(f"CREATE DATABASE {quote_identifier(name)}")
    try:
        dsns = [conninfo.make_conninfo(base_dsn, dbname=name) for name in templates]
        for dsn in dsns:
            with database.connect(dsn, autocommit=True) as connection:
                connection.execute(SCHEMA_SQL)

        def run(dsn: str) -> MutationResult:
            return run_mutation_tests(
                mutations,
                schema_file=schema_file,
                database_url=dsn,
                pytest_args=[str(test_file), "-q", "-p", "no:cacheprovider"],
                env_var="SQLPROOF_MUTATION_TEST_DSN",
                release_template_sessions=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, dsns))

        for result in results:
            # Killed means the inner suite ran to completion on its own clone;
            # a clone dropped mid-run would surface as an "error" (or a red
            # baseline raising SqlProofMutationError above).
            assert [o.status for o in result.outcomes] == ["killed"], result.to_dict()
    finally:
        with database.connect(base_dsn, autocommit=True) as admin:
            for name in templates:
                admin.execute(f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE)")
