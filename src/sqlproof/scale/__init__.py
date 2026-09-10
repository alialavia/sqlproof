"""Scale measurement: is this function's growth curve acceptable?

The output is an assertion, not a report -- CI goes red when someone
writes a query that will not survive growth, the way
`assert_no_survivors()` works for mutation testing.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.mutation.persist import capture_git_info
from sqlproof.scale.args import heaviest, median_key, random_key
from sqlproof.scale.artifact import save_run
from sqlproof.scale.result import ScaleResult
from sqlproof.scale.sweep import run_sweep

if TYPE_CHECKING:
    from sqlproof.core import SqlProof

__all__ = [
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
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    artifact_dir: Path | str | None = ".sqlproof/scale-runs",
    truncate_existing: bool = False,
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
    `probe_timeout_s` (default 30.0; checked after each probe returns,
    never cancelling one), `seed` (default 0) and `columns` (per-column
    overrides, as `load_dataset` takes them).

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
    with psycopg.connect(dsn, autocommit=True) as conn:
        result = run_sweep(
            conn, proof.schema_info, function,
            sizes=sizes, args=args, truncate_existing=truncate_existing, **kwargs,
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
