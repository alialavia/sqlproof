from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlproof.mutation.result import MutantOutcome, Status

SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(Exception):
    """Raised when an artifact declares a schema_version this build cannot read."""


@dataclass(frozen=True, slots=True)
class RunArtifact:
    run_id: str
    started_at: str  # ISO-8601 UTC, e.g. "2026-06-11T14:32:05Z"
    duration_s: float
    sqlproof_version: str
    git_sha: str | None
    git_dirty: bool
    hypothesis_seed: int | None
    schema_fingerprint: str | None
    pytest_args: tuple[str, ...]
    outcomes: tuple[MutantOutcome, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "sqlproof_version": self.sqlproof_version,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "hypothesis_seed": self.hypothesis_seed,
            "schema_fingerprint": self.schema_fingerprint,
            "pytest_args": list(self.pytest_args),
            "outcomes": [_outcome_to_dict(o) for o in self.outcomes],
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> RunArtifact:
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            msg = f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})"
            raise UnsupportedSchemaVersion(msg)
        return cls(
            run_id=str(payload["run_id"]),
            started_at=str(payload["started_at"]),
            duration_s=float(payload["duration_s"]),
            sqlproof_version=str(payload["sqlproof_version"]),
            git_sha=payload["git_sha"],
            git_dirty=bool(payload["git_dirty"]),
            hypothesis_seed=payload["hypothesis_seed"],
            schema_fingerprint=payload["schema_fingerprint"],
            pytest_args=tuple(payload["pytest_args"]),
            outcomes=tuple(_outcome_from_dict(o) for o in payload["outcomes"]),
        )


def _outcome_to_dict(outcome: MutantOutcome) -> dict[str, Any]:
    return {
        "mutant_id": outcome.mutant_id,
        "target": outcome.target,
        "description": outcome.description,
        "status": outcome.status,
        "pytest_exit_code": outcome.pytest_exit_code,
        "hypothesis_seed": outcome.hypothesis_seed,
        "detail": outcome.detail,
        "duration_s": outcome.duration_s,
    }


def _outcome_from_dict(payload: dict[str, Any]) -> MutantOutcome:
    return MutantOutcome(
        mutant_id=str(payload["mutant_id"]),
        target=str(payload["target"]),
        description=str(payload["description"]),
        status=cast(Status, payload["status"]),
        pytest_exit_code=payload["pytest_exit_code"],
        hypothesis_seed=payload["hypothesis_seed"],
        detail=payload["detail"],
        duration_s=payload.get("duration_s"),
    )
