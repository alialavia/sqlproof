"""Persist a scale run as JSON.

Same conventions as the mutation-run artifacts (schema_version, git sha,
schema fingerprint) so a single ingester can consume both. This is the
wire format a cloud tier would read; the local file is a side effect of
running the assertion.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlproof._version import __version__
from sqlproof.mutation.persist import capture_git_info, new_run_id
from sqlproof.scale.result import ScaleResult

SCHEMA_VERSION = 1

# started_at is ISO-8601 with ':' which is illegal in filenames on some
# platforms; replace with '-' so the timestamp still sorts chronologically.
_FILENAME_SAFE = str.maketrans({":": "-"})
_UNSAFE_FUNCTION_CHARS = re.compile(r"[^A-Za-z0-9_]")


def save_run(
    result: ScaleResult,
    artifact_dir: Path,
    *,
    schema_fingerprint: str | None = None,
    started_at: datetime | None = None,
    duration_s: float | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> Path:
    """Write *result* as one JSON file under *artifact_dir* and return the path.

    Mirrors `sqlproof.mutation.persist.save_run`'s envelope (schema_version,
    run_id, git sha/dirty, sqlproof_version, schema_fingerprint) so a single
    ingester can read both artifact families.

    The directory is created if missing. `started_at` defaults to now (UTC)
    when not given; pass the time the sweep actually started for an accurate
    trend history. The directory is append-only: this never rewrites an
    existing run (exclusive create -- a genuine collision raises
    `FileExistsError`).

    `git_sha` / `git_dirty` should describe the code that was MEASURED:
    pass the pair `capture_git_info()` returned before the sweep started,
    as `scale_analysis` does -- a sweep can run long enough for the
    working tree to change underneath it. Only when neither is given are
    they captured here, at save time.

    The artifact carries what a re-fit needs, with no database (Ruling
    AQ): `points` holds every measured ProbePoint and `baseline` the
    calibrated fixed per-call cost the fit subtracted. Segmenting the
    points on `plan_hash` (`fit.segment_by_plan`) and calling
    `fit.fit_exponent(segment, baseline)` reproduces each entry of
    `regimes`. `seed`, `max_factor`, `min_points` and `probe_timeout_s`
    record how the sweep ran, so it can be run again: the same seed and
    profile generate the same data, and so the same buffer counts.

    `argument_policy` holds one entry per argument position, saying how
    that argument was chosen (Ruling AO): `{"kind": "heaviest", "column":
    "t.id"}` (the largest key value), `{"kind": "median_key", "column":
    ...}`, `{"kind": "random_key", "column": ..., "seed": 0}`, `{"kind":
    "callable"}` for a caller's own resolver, or `{"kind": "literal"}`
    (the value itself is in each point's `args`). No entry claims a
    worst case: none of the resolvers guarantees one.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    when = started_at if started_at is not None else datetime.now(UTC)
    started_at_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = new_run_id()
    if git_sha is None and git_dirty is None:
        git_sha, git_dirty = capture_git_info()

    stamp = started_at_str.translate(_FILENAME_SAFE)
    safe_function = _UNSAFE_FUNCTION_CHARS.sub("_", result.function)
    path = artifact_dir / f"{stamp}-{safe_function}-{run_id}.json"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at_str,
        "duration_s": duration_s,
        "sqlproof_version": __version__,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "schema_fingerprint": schema_fingerprint,
        "function": result.function,
        "sizes": dict(result.sizes),
        "truncated": result.truncated,
        "baseline": result.baseline,
        "seed": result.seed,
        "max_factor": result.max_factor,
        "min_points": result.min_points,
        "probe_timeout_s": result.probe_timeout_s,
        # One entry per argument position (see the docstring) -- deliberately
        # not a run-wide "worst case" label: no resolver guarantees one.
        "argument_policy": [dict(entry) for entry in result.argument_policy],
        "spill_point_rows": result.spill_point_rows,
        "factor_at_spill": result.factor_at_spill,
        "points": [
            {**asdict(point), "args": list(point.args)} for point in result.points
        ],
        "regimes": [asdict(regime) for regime in result.regimes],
        "plan_flips": [asdict(flip) for flip in result.plan_flips],
    }
    # Exclusive create ('x'): honor the append-only contract -- never
    # silently overwrite an existing run.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return path
