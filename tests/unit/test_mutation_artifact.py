from __future__ import annotations

from sqlproof.mutation.artifact import SCHEMA_VERSION, RunArtifact
from sqlproof.mutation.result import MutantOutcome


def _artifact() -> RunArtifact:
    return RunArtifact(
        run_id="a1b2c3d4",
        started_at="2026-06-11T14:32:05Z",
        duration_s=412.7,
        sqlproof_version="0.9.0",
        git_sha="58d0e84",
        git_dirty=False,
        hypothesis_seed=1234567890,
        schema_fingerprint="sha256:abc",
        pytest_args=("-m", "rls", "tests/"),
        outcomes=(
            MutantOutcome(
                mutant_id="m1",
                target="billing.compute_invoice",
                description="COALESCE(SUM(usage), 0) -> 1",
                status="killed",
                pytest_exit_code=1,
                hypothesis_seed=1234567890,
                detail=None,
                duration_s=8.3,
            ),
        ),
    )


def test_round_trips_through_json_dict() -> None:
    original = _artifact()
    restored = RunArtifact.from_json_dict(original.to_json_dict())
    assert restored == original


def test_to_json_dict_stamps_schema_version() -> None:
    payload = _artifact().to_json_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["outcomes"][0]["duration_s"] == 8.3


def test_from_json_dict_rejects_unknown_schema_version() -> None:
    import pytest

    from sqlproof.mutation.artifact import UnsupportedSchemaVersion

    payload = _artifact().to_json_dict()
    payload["schema_version"] = 999
    with pytest.raises(UnsupportedSchemaVersion):
        RunArtifact.from_json_dict(payload)


def test_round_trips_none_optionals_and_dirty_flag() -> None:
    artifact = RunArtifact(
        run_id="x",
        started_at="2026-06-11T14:32:05Z",
        duration_s=1.0,
        sqlproof_version="0.9.0",
        git_sha=None,
        git_dirty=True,
        hypothesis_seed=None,
        schema_fingerprint=None,
        pytest_args=(),
        outcomes=(
            MutantOutcome(
                mutant_id="m1",
                target="f",
                description="f: drop",
                status="error",
                pytest_exit_code=None,
                hypothesis_seed=None,
                detail=None,
                duration_s=None,
            ),
        ),
    )
    restored = RunArtifact.from_json_dict(artifact.to_json_dict())
    assert restored == artifact
    assert restored.git_sha is None
    assert restored.git_dirty is True
    assert restored.outcomes[0].duration_s is None
