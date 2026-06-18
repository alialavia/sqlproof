from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import conninfo, sql

from sqlproof._version import __version__
from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import PreparedMutant, prepare_mutants
from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.model import MutationSet
from sqlproof.mutation.persist import capture_git_info, new_run_id, save_run
from sqlproof.mutation.result import MutantOutcome, MutationResult, outcome_for_exit_code
from sqlproof.schema.fingerprint import compute as compute_fingerprint
from sqlproof.schema.parse_sql import parse_schema_sql

_OUTPUT_TAIL = 2000


def _resolve_seed(seed: int | None) -> int:
    """Return *seed* unchanged if given, otherwise generate a fresh random seed.

    The returned value is always in ``range(2**32)`` so it is safe to pass
    directly to ``--hypothesis-seed``.
    """
    if seed is not None:
        return seed
    return secrets.randbelow(2**32)


class LocalMutationRunner:
    """Clone-per-mutant local execution.

    Each mutant gets a fresh database created from the template (the
    `database_url` database), the mutated DDL applied, and one pytest
    subprocess run with `env_var` pointing at the clone. The clone is
    dropped afterwards — there is no restore path by design.

    NOTE: `CREATE DATABASE ... TEMPLATE` requires the template database
    to have no other connections. Close dev sessions against it first.
    Clone creation is serialized behind a lock for the same reason.

    Interrupted runs may leave ``sqlproof_mutant_*`` databases behind.
    Rerunning the same mutation set self-heals (each run drops-then-recreates
    by id); edited mutants receive new ids, so orphans from those must be
    dropped manually (e.g. ``DROP DATABASE IF EXISTS sqlproof_mutant_<id>``).

    The pytest child process inherits the parent environment, so any
    ``PYTEST_ADDOPTS`` set in CI will affect mutant suites — review that
    variable when debugging unexpected pytest behaviour inside mutation runs.
    """

    def __init__(
        self,
        *,
        database_url: str,
        pytest_args: Sequence[str],
        env_var: str = "SQLPROOF_TEST_DATABASE_URL",
        maintenance_db: str = "postgres",
        hypothesis_seed: int | None = None,
        max_workers: int = 1,
        timeout_s: float | None = 600.0,
    ) -> None:
        self.database_url = database_url
        self.pytest_args = list(pytest_args)
        self.env_var = env_var
        self.maintenance_db = maintenance_db
        self.hypothesis_seed = hypothesis_seed
        self.max_workers = max(1, max_workers)
        self.timeout_s = timeout_s
        self._clone_lock = threading.Lock()

    # -- orchestration ------------------------------------------------

    def run(self, prepared: Sequence[PreparedMutant]) -> MutationResult:
        if self.max_workers == 1:
            outcomes = [self._run_one(p) for p in prepared]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                outcomes = list(pool.map(self._run_one, prepared))
        return MutationResult(outcomes=tuple(outcomes))

    def _run_one(self, prepared: PreparedMutant) -> MutantOutcome:
        clone_name = f"sqlproof_mutant_{prepared.mutant_id}"
        start = time.monotonic()
        try:
            clone_dsn = self._create_clone(clone_name)
            try:
                self._apply_ddl(clone_dsn, prepared.ddl)
                exit_code, output = self._run_pytest(clone_dsn)
            finally:
                self._drop_clone(clone_name)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if exc.__context__ is not None:
                detail += f" (during handling of: {exc.__context__!r})"
            return MutantOutcome(
                mutant_id=prepared.mutant_id,
                target=prepared.mutant.target_name,
                description=prepared.mutant.describe(),
                status="error",
                pytest_exit_code=None,
                hypothesis_seed=self.hypothesis_seed,
                detail=detail,
                duration_s=time.monotonic() - start,
            )
        return outcome_for_exit_code(
            mutant_id=prepared.mutant_id,
            target=prepared.mutant.target_name,
            description=prepared.mutant.describe(),
            expect_survives=prepared.mutant.expect_survives,
            exit_code=exit_code,
            hypothesis_seed=self.hypothesis_seed,
            detail=None if exit_code == 1 else output[-_OUTPUT_TAIL:],
            duration_s=time.monotonic() - start,
        )

    # -- side-effecting seams (overridden in unit tests) ---------------

    def _create_clone(self, clone_name: str) -> str:
        template = self._dbname(self.database_url)
        with self._clone_lock, psycopg.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(clone_name)
                )
            )
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(clone_name), sql.Identifier(template)
                )
            )
        return self._dsn_for(clone_name)

    def _drop_clone(self, clone_name: str) -> None:
        with self._clone_lock, psycopg.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(clone_name)
                )
            )

    def _apply_ddl(self, clone_dsn: str, ddl: str) -> None:
        # ddl is program-generated (from build_mutated_ddl), not user input;
        # cast to LiteralString satisfies pyright strict's sql.SQL() requirement.
        with psycopg.connect(clone_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL(cast(LiteralString, ddl)))  # type: ignore[redundant-cast]

    def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
        env: dict[str, str] = dict(os.environ) | {self.env_var: clone_dsn}
        process = subprocess.run(
            self._pytest_command(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.timeout_s,
        )
        return process.returncode, process.stdout + process.stderr

    # -- helpers --------------------------------------------------------

    def _pytest_command(self) -> list[str]:
        command = [sys.executable, "-m", "pytest", *self.pytest_args]
        if self.hypothesis_seed is not None:
            command.append(f"--hypothesis-seed={self.hypothesis_seed}")
        return command

    def _dsn_for(self, dbname: str) -> str:
        # make_conninfo accepts an existing conninfo/URL string and overrides;
        # passing dbname= as a keyword arg avoids unpacking ConnDict which
        # mypy strict does not accept as **kwargs: ConnParam.
        return conninfo.make_conninfo(self.database_url, dbname=dbname)

    def _dbname(self, dsn: str) -> str:
        dbname = conninfo.conninfo_to_dict(dsn).get("dbname")
        if not dbname:
            msg = f"database_url has no dbname: {dsn!r}"
            raise SqlProofMutationError(msg)
        return str(dbname)


def run_mutation_tests(
    mutations: MutationSet,
    *,
    schema_file: str | Path,
    database_url: str,
    pytest_args: Sequence[str],
    env_var: str = "SQLPROOF_TEST_DATABASE_URL",
    maintenance_db: str = "postgres",
    hypothesis_seed: int | None = None,
    max_workers: int = 1,
    artifact_dir: str | Path | None = None,
    timeout_s: float | None = 600.0,
) -> MutationResult:
    """Prepare every mutant (all authoring errors raise here, before any
    database work), then run each against a fresh clone of `database_url`.

    `database_url` is the template database: schema applied, no
    connections open. `pytest_args` selects the suite; the subprocess
    sees `env_var` pointing at the per-mutant clone.

    `maintenance_db` is the always-on database used for ``CREATE DATABASE``
    and ``DROP DATABASE`` commands. It must be reachable with the same
    credentials as `database_url` — the default ``"postgres"`` is correct
    for most setups; override when the cluster exposes a different
    always-available database (e.g. ``"template1"`` or the CI service name).

    `max_workers` controls how many clone-create/pytest subprocesses run in
    parallel. Clone creation and deletion serialize on an internal lock
    (``CREATE DATABASE ... TEMPLATE`` requires the template to have zero
    connections), so the actual concurrency is in the pytest subprocesses
    themselves. Default is ``1`` (sequential).

    `hypothesis_seed` pins the Hypothesis seed for every mutant run,
    making failures reproducible. If ``None`` (the default), a fresh
    random seed is generated via :func:`secrets.randbelow` so every
    invocation is pinned to a concrete, replayable value — check the
    ``hypothesis_seed`` field on each :class:`~sqlproof.mutation.result.MutantOutcome`
    to replay a specific run.

    `timeout_s` is the per-mutant pytest timeout in seconds (default
    600 s / 10 minutes). Pass ``None`` to disable. A hung mutant —
    the canonical mutation-testing failure for a mutated loop condition —
    raises :class:`subprocess.TimeoutExpired`, which flows through to an
    ``"error"`` outcome.

    `artifact_dir`, when given, persists this run as a JSON artifact under
    that directory (created if missing) via :func:`save_run`, capturing the
    resolved seed, schema fingerprint, git sha/dirty flag, and per-mutant
    outcomes. When ``None`` (the default) nothing is written. Point
    ``sqlproof mutation report`` at the same directory to visualize history.

    .. note:: **PYTEST_ADDOPTS**
        The pytest child process inherits the parent environment. If
        ``PYTEST_ADDOPTS`` is set (e.g. by CI to add ``--tb=short`` or
        ``--no-header``), those flags affect every mutant suite. Review
        that variable when debugging unexpected pytest behaviour inside
        mutation runs.
    """
    schema_sql = Path(schema_file).read_text(encoding="utf-8")
    prepared = prepare_mutants(mutations, schema_sql)
    hypothesis_seed = _resolve_seed(hypothesis_seed)
    runner = LocalMutationRunner(
        database_url=database_url,
        pytest_args=pytest_args,
        env_var=env_var,
        maintenance_db=maintenance_db,
        hypothesis_seed=hypothesis_seed,
        max_workers=max_workers,
        timeout_s=timeout_s,
    )
    started = datetime.now(timezone.utc)
    monotonic_start = time.monotonic()
    result = runner.run(prepared)
    run_duration_s = time.monotonic() - monotonic_start
    if artifact_dir is not None:
        try:
            git_sha, git_dirty = capture_git_info()
            try:
                fingerprint: str | None = compute_fingerprint(parse_schema_sql(schema_sql))
            except Exception:
                fingerprint = None
            artifact = RunArtifact(
                run_id=new_run_id(),
                started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_s=run_duration_s,
                sqlproof_version=__version__,
                git_sha=git_sha,
                git_dirty=git_dirty,
                hypothesis_seed=hypothesis_seed,
                schema_fingerprint=fingerprint,
                pytest_args=tuple(pytest_args),
                outcomes=result.outcomes,
            )
            save_run(artifact, artifact_dir=Path(artifact_dir))
        except OSError as exc:
            warnings.warn(
                f"mutation run completed but artifact could not be written to "
                f"{artifact_dir}: {exc}",
                stacklevel=2,
            )
    return result
