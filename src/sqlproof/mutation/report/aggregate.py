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
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(RunArtifact.from_json_dict(payload))
        except (json.JSONDecodeError, UnsupportedSchemaVersion, KeyError, ValueError, TypeError) as exc:
            skipped.append(SkippedFile(path=path, reason=f"{type(exc).__name__}: {exc}"))
    runs.sort(key=lambda r: r.started_at)
    return LoadResult(runs=runs, skipped=skipped)
