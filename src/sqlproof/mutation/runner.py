from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import conninfo, sql

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import PreparedMutant, prepare_mutants
from sqlproof.mutation.model import MutationSet
from sqlproof.mutation.result import MutantOutcome, MutationResult, outcome_for_exit_code

_OUTPUT_TAIL = 2000


class LocalMutationRunner:
    """Clone-per-mutant local execution.

    Each mutant gets a fresh database created from the template (the
    `database_url` database), the mutated DDL applied, and one pytest
    subprocess run with `env_var` pointing at the clone. The clone is
    dropped afterwards — there is no restore path by design.

    NOTE: `CREATE DATABASE ... TEMPLATE` requires the template database
    to have no other connections. Close dev sessions against it first.
    Clone creation is serialized behind a lock for the same reason.
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
    ) -> None:
        self.database_url = database_url
        self.pytest_args = list(pytest_args)
        self.env_var = env_var
        self.maintenance_db = maintenance_db
        self.hypothesis_seed = hypothesis_seed
        self.max_workers = max(1, max_workers)
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
        try:
            clone_dsn = self._create_clone(clone_name)
            try:
                self._apply_ddl(clone_dsn, prepared.ddl)
                exit_code, output = self._run_pytest(clone_dsn)
            finally:
                self._drop_clone(clone_name)
        except Exception as exc:
            return MutantOutcome(
                mutant_id=prepared.mutant_id,
                target=prepared.mutant.target_name,
                description=prepared.mutant.describe(),
                status="error",
                pytest_exit_code=None,
                hypothesis_seed=self.hypothesis_seed,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return outcome_for_exit_code(
            mutant_id=prepared.mutant_id,
            target=prepared.mutant.target_name,
            description=prepared.mutant.describe(),
            expect_survives=prepared.mutant.expect_survives,
            exit_code=exit_code,
            hypothesis_seed=self.hypothesis_seed,
            detail=None if exit_code == 1 else output[-_OUTPUT_TAIL:],
        )

    # -- side-effecting seams (overridden in unit tests) ---------------

    def _create_clone(self, clone_name: str) -> str:
        template = self._dbname(self.database_url)
        with self._clone_lock, psycopg.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(clone_name))
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
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(clone_name))
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
) -> MutationResult:
    """Prepare every mutant (all authoring errors raise here, before any
    database work), then run each against a fresh clone of `database_url`.

    `database_url` is the template database: schema applied, no
    connections open. `pytest_args` selects the suite; the subprocess
    sees `env_var` pointing at the per-mutant clone.
    """
    schema_sql = Path(schema_file).read_text(encoding="utf-8")
    prepared = prepare_mutants(mutations, schema_sql)
    runner = LocalMutationRunner(
        database_url=database_url,
        pytest_args=pytest_args,
        env_var=env_var,
        maintenance_db=maintenance_db,
        hypothesis_seed=hypothesis_seed,
        max_workers=max_workers,
    )
    return runner.run(prepared)
