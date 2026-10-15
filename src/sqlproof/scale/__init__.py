"""Scale measurement: is this function's growth curve acceptable?

The output is an assertion, not a report: CI goes red when a function's
BUFFER WORK -- the pages its queries touch -- grows faster than the test
allows, the way `assert_no_survivors()` works for mutation testing.

Two blind spots bound that claim (see the design spec's "Known
limitations"). Buffers count I/O-visible work, not CPU, so a quadratic
that re-reads nothing -- a non-equi join over a materialized inner side
-- fits ~1. And EXPLAIN of `SELECT fn()` shows only the outer statement,
so a plan change inside a non-inlined function goes unseen.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlproof import _database as database
from sqlproof.exceptions import SqlProofScaleError, SqlProofUsageError
from sqlproof.mutation.persist import capture_git_info
from sqlproof.scale.advisors import AdvisorHit
from sqlproof.scale.args import heaviest, median_key, random_key
from sqlproof.scale.artifact import save_run
from sqlproof.scale.result import ScaleResult
from sqlproof.scale.sweep import run_sweep

if TYPE_CHECKING:
    from sqlproof.core import SqlProof
    from sqlproof.scenarios.model import Scenario

__all__ = [
    "AdvisorHit",
    "ScaleResult",
    "heaviest",
    "median_key",
    "random_key",
    "scale_analysis",
]


def scale_analysis(
    proof: SqlProof,
    function: str,
    *,
    sizes: Mapping[str, int] | None = None,
    scenario: Scenario | None = None,
    args: Sequence[Any] = (),
    artifact_dir: Path | str | None = ".sqlproof/scale-runs",
    truncate_existing: bool = False,
    in_clone: bool = False,
    release_template_sessions: bool = False,
    **kwargs: Any,
) -> ScaleResult:
    """Measure how `function` scales, using `proof`'s schema and database.

    DESTRUCTIVE -- point it at a dedicated test database. The sweep
    EMPTIES AND REPOPULATES every table in the proof's schema
    (`proof.schema_info`: for a connection-string proof, every table in
    `SqlProofConfig.schema`), committing as it goes. It refuses to start
    while any of those tables holds rows, unless `truncate_existing=True`
    -- deliberately an explicit keyword here, not one of `**kwargs`, so
    the opt-in to deleting rows is always visible at the call site. It
    leaves the tables EMPTY when it finishes, whether it succeeded or
    failed. See `run_sweep` for everything it checks before the first
    TRUNCATE.

    Options forwarded to `run_sweep` through `**kwargs`: `max_factor`
    (default 32), `min_points` (default 5; at least `fit.MIN_POINTS`),
    `probe_timeout_s` (default 30.0; Postgres cancels a probe that runs
    longer, and the sweep ends truncated), `seed` (default 0), `columns` (per-column
    overrides, as `load_dataset` takes them), `distribution` (`"uniform"`,
    the default, or `"zipf"`), `nested_plans` (count the plans of the
    statements inside the function, through auto_explain),
    `scale_cohorts` and `advisors`.

    `advisors=True` (default False) also ranks Supabase `index_advisor`
    suggestions for the statements the function runs, computed once on
    the largest factor's data, into `ScaleResult.advisor_hits` and the
    artifact. It uses the extension only when it is already installed in
    the database (`CREATE EXTENSION index_advisor CASCADE`; with
    `in_clone=True` the copy inherits it) and never creates it; when it
    is missing, or a statement cannot be planned, `advisor_notes` says
    why and the measurement stands. Without `nested_plans=True` it sees
    only the outer `SELECT fn(...)` call, which helps only for functions
    Postgres inlines.

    Pass `scenario=` instead of `sizes=` to sweep a scenario: its fills and
    per-parent counts grow with the factor while its cohorts and named
    rows stay fixed, and `ref("name")` arguments resolve to named rows at
    every factor. See `run_sweep`.

    With `in_clone=True` the sweep runs in a throwaway copy of the
    database (`CREATE DATABASE ... TEMPLATE`, dropped afterwards), so the
    proof's own tables are never touched, whatever they hold, and
    `truncate_existing` is not needed. It needs the CREATEDB privilege
    and no other sessions on the database while it is copied; behind a
    pooler such as Supavisor, `release_template_sessions=True` closes
    this role's idle sessions there first, as mutation testing does.

    Opens its own autocommit connection: the sweep issues TRUNCATE,
    COPY and ANALYZE, which want a lifecycle of their own rather than
    sharing the property runner's transaction.

    Writes a JSON run artifact under `artifact_dir` (default
    `.sqlproof/scale-runs`, relative to the current working directory)
    for trend history, mirroring mutation-run artifacts; pass
    `artifact_dir=None` to skip. A write failure warns rather than
    failing the assertion the run exists for.
    """
    dsn = proof.config.connection_string
    if dsn is None:
        msg = (
            "scale_analysis needs a real database: construct SqlProof with "
            "a connection_string rather than a schema file."
        )
        raise SqlProofUsageError(msg)
    # Capture the start time before the sweep, not the save time, so the
    # artifact records when the measurement actually began.
    started = datetime.now(UTC)
    monotonic_start = time.monotonic()
    # Git state too, and for the same reason: the artifact must describe
    # the code that was measured, and a sweep can run long enough for the
    # working tree to change underneath it (Ruling AQ).
    git_sha: str | None = None
    git_dirty = False
    if artifact_dir is not None:
        git_sha, git_dirty = capture_git_info()
    if in_clone:
        from sqlproof._clone import TemplateCloner
        from sqlproof.mutation.persist import new_run_id

        cloner = TemplateCloner(
            database_url=dsn,
            run_id=new_run_id(),
            error=SqlProofScaleError,
            release_template_sessions=release_template_sessions,
        )
        clone = cloner.clone_name("sqlproof_scale")
        clone_dsn = cloner.create(clone)
        try:
            with database.connect(clone_dsn, autocommit=True) as conn:
                result = run_sweep(
                    conn, proof.schema_info, function,
                    sizes=sizes, scenario=scenario, args=args,
                    truncate_existing=True, **kwargs,
                )
        finally:
            cloner.drop(clone)
    else:
        with database.connect(dsn, autocommit=True) as conn:
            result = run_sweep(
                conn, proof.schema_info, function,
                sizes=sizes, scenario=scenario, args=args,
                truncate_existing=truncate_existing, **kwargs,
            )
    duration_s = time.monotonic() - monotonic_start
    if artifact_dir is not None:
        try:
            save_run(
                result,
                Path(artifact_dir),
                schema_fingerprint=proof.schema_fingerprint,
                started_at=started,
                duration_s=duration_s,
                git_sha=git_sha,
                git_dirty=git_dirty,
            )
        except OSError as exc:
            warnings.warn(
                f"scale run completed but artifact could not be written to "
                f"{artifact_dir}: {exc}",
                stacklevel=2,
            )
    return result
