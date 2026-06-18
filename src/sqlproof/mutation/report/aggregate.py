from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlproof.mutation.artifact import RunArtifact, UnsupportedSchemaVersion


@dataclass(frozen=True, slots=True)
class SkippedFile:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class LoadResult:
    runs: list[RunArtifact]
    skipped: list[SkippedFile]


def load_runs(runs_dir: Path) -> LoadResult:
    """Read every ``*.json`` artifact in *runs_dir*, sorted chronologically by
    ``started_at``. Unparseable files and unknown schema versions are skipped
    (recorded in ``skipped``), never raised — one bad artifact must not kill
    the report. A missing directory yields an empty result."""
    runs: list[RunArtifact] = []
    skipped: list[SkippedFile] = []
    if not runs_dir.is_dir():
        return LoadResult(runs=[], skipped=[])
    # sorted() makes the skipped-file order deterministic (glob order is
    # filesystem-dependent); runs themselves are re-sorted by started_at below.
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(RunArtifact.from_json_dict(payload))
        # All reachable from from_json_dict on syntactically valid but malformed
        # JSON; skip-not-raise keeps one bad artifact from killing the report.
        except (json.JSONDecodeError, UnsupportedSchemaVersion, KeyError, ValueError, TypeError) as exc:
            skipped.append(SkippedFile(path=path, reason=f"{type(exc).__name__}: {exc}"))
    runs.sort(key=lambda r: r.started_at)
    return LoadResult(runs=runs, skipped=skipped)


@dataclass(frozen=True, slots=True)
class TrendPoint:
    started_at: str
    score: float | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    started_at: str
    git_sha: str | None
    git_dirty: bool
    duration_s: float
    killed: int
    survived: int
    errored: int
    score: float | None
    schema_fingerprint: str | None
    schema_changed: bool


@dataclass(frozen=True, slots=True)
class TargetSummary:
    target: str
    latest_score: float | None
    mutant_count: int
    survivor_count: int
    history: list[TrendPoint]


@dataclass(frozen=True, slots=True)
class SurvivorEntry:
    mutant_id: str
    target: str
    description: str
    repro_command: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class ReportData:
    runs: list[RunSummary]
    targets: list[TargetSummary]
    latest_survivors: list[SurvivorEntry]
    skipped: list[SkippedFile]


_SURVIVED = "survived"
_NUMERATOR = frozenset({"killed", "unexpected_kill"})
_DENOMINATOR = frozenset({"killed", "unexpected_kill", "survived"})


def _score(outcomes) -> float | None:  # type: ignore[no-untyped-def]
    denominator = sum(1 for o in outcomes if o.status in _DENOMINATOR)
    if denominator == 0:
        return None
    numerator = sum(1 for o in outcomes if o.status in _NUMERATOR)
    return numerator / denominator


def _repro_command(pytest_args, seed, mutant_id) -> str:  # type: ignore[no-untyped-def]
    args = " ".join(pytest_args)
    seed_flag = f" --hypothesis-seed={seed}" if seed is not None else ""
    return f"pytest {args}{seed_flag}  # mutant {mutant_id}"


def build_report(load_result: LoadResult) -> ReportData:
    """Turn loaded artifacts (chronological) into the dashboard view model:
    per-run scores, per-target history, latest-run survivors classified as
    new vs known, and schema-drift flags. Pure — no IO."""
    runs = load_result.runs
    run_summaries: list[RunSummary] = []
    prev_fingerprint: str | None = None
    for index, run in enumerate(runs):
        run_summaries.append(
            RunSummary(
                run_id=run.run_id,
                started_at=run.started_at,
                git_sha=run.git_sha,
                git_dirty=run.git_dirty,
                duration_s=run.duration_s,
                killed=sum(1 for o in run.outcomes if o.status in ("killed", "unexpected_kill")),
                survived=sum(1 for o in run.outcomes if o.status == _SURVIVED),
                errored=sum(1 for o in run.outcomes if o.status == "error"),
                score=_score(run.outcomes),
                schema_fingerprint=run.schema_fingerprint,
                schema_changed=index > 0 and run.schema_fingerprint != prev_fingerprint,
            )
        )
        prev_fingerprint = run.schema_fingerprint

    targets = _build_targets(runs)
    latest_survivors = _build_latest_survivors(runs)
    return ReportData(
        runs=run_summaries,
        targets=targets,
        latest_survivors=latest_survivors,
        skipped=load_result.skipped,
    )


def _build_targets(runs) -> list[TargetSummary]:  # type: ignore[no-untyped-def]
    names = sorted({o.target for run in runs for o in run.outcomes})
    summaries: list[TargetSummary] = []
    for name in names:
        history = [
            TrendPoint(
                started_at=run.started_at,
                score=_score([o for o in run.outcomes if o.target == name]),
            )
            for run in runs
            if any(o.target == name for o in run.outcomes)
        ]
        latest_run = next(
            (run for run in reversed(runs) if any(o.target == name for o in run.outcomes)),
            None,
        )
        latest_outcomes = (
            [o for o in latest_run.outcomes if o.target == name] if latest_run else []
        )
        summaries.append(
            TargetSummary(
                target=name,
                latest_score=_score(latest_outcomes),
                mutant_count=len(latest_outcomes),
                survivor_count=sum(1 for o in latest_outcomes if o.status == _SURVIVED),
                history=history,
            )
        )
    return summaries


def _build_latest_survivors(runs) -> list[SurvivorEntry]:  # type: ignore[no-untyped-def]
    if not runs:
        return []
    latest = runs[-1]
    earlier_survivor_ids = {
        o.mutant_id
        for run in runs[:-1]
        for o in run.outcomes
        if o.status == _SURVIVED
    }
    entries: list[SurvivorEntry] = []
    for outcome in latest.outcomes:
        if outcome.status != _SURVIVED:
            continue
        entries.append(
            SurvivorEntry(
                mutant_id=outcome.mutant_id,
                target=outcome.target,
                description=outcome.description,
                repro_command=_repro_command(
                    latest.pytest_args, outcome.hypothesis_seed, outcome.mutant_id
                ),
                is_new=outcome.mutant_id not in earlier_survivor_ids,
            )
        )
    return entries
