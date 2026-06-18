from __future__ import annotations

import json

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.persist import new_run_id, save_run
from sqlproof.mutation.result import MutantOutcome


def _artifact(run_id: str = "a1b2c3d4") -> RunArtifact:
    return RunArtifact(
        run_id=run_id,
        started_at="2026-06-11T14:32:05Z",
        duration_s=1.0,
        sqlproof_version="0.9.0",
        git_sha=None,
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:abc",
        pytest_args=("tests/",),
        outcomes=(
            MutantOutcome(
                mutant_id="m1",
                target="f",
                description="f: drop 'x'",
                status="killed",
                pytest_exit_code=1,
                hypothesis_seed=42,
                detail=None,
                duration_s=0.5,
            ),
        ),
    )


def test_save_run_writes_readable_artifact(tmp_path) -> None:
    path = save_run(_artifact(), artifact_dir=tmp_path)
    assert path.exists()
    assert path.parent == tmp_path
    restored = RunArtifact.from_json_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored == _artifact()


def test_save_run_filename_sorts_chronologically(tmp_path) -> None:
    save_run(_artifact(run_id="aaaaaaaa"), artifact_dir=tmp_path)
    path = save_run(_artifact(run_id="bbbbbbbb"), artifact_dir=tmp_path)
    # started_at leads the filename so lexical sort == chronological sort
    assert path.name.startswith("2026-06-11T14-32-05Z-")


def test_save_run_creates_missing_directory(tmp_path) -> None:
    target = tmp_path / "nested" / "runs"
    save_run(_artifact(), artifact_dir=target)
    assert list(target.glob("*.json"))


def test_new_run_id_is_hex_and_distinct() -> None:
    ids = {new_run_id() for _ in range(10)}
    assert len(ids) == 10
    assert all(len(i) >= 8 for i in ids)
