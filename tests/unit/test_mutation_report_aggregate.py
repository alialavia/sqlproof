from __future__ import annotations

import json

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.report.aggregate import load_runs
from sqlproof.mutation.result import MutantOutcome


def _write(dir_path, run_id, started_at, outcomes=()):  # type: ignore[no-untyped-def]
    artifact = RunArtifact(
        run_id=run_id,
        started_at=started_at,
        duration_s=1.0,
        sqlproof_version="0.9.0",
        git_sha="abc1234",
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:s1",
        pytest_args=("tests/",),
        outcomes=tuple(outcomes),
    )
    path = dir_path / f"{started_at.replace(':', '-')}-{run_id[:6]}.json"
    path.write_text(json.dumps(artifact.to_json_dict()), encoding="utf-8")
    return path


def test_load_runs_returns_runs_sorted_by_started_at(tmp_path) -> None:
    _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z")
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z")
    loaded = load_runs(tmp_path)
    assert [r.started_at for r in loaded.runs] == [
        "2026-06-11T10:00:00Z",
        "2026-06-12T10:00:00Z",
    ]
    assert loaded.skipped == []


def test_load_runs_skips_corrupt_and_unknown_version(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    bad_version = tmp_path / "bad-version.json"
    payload = json.loads((next(tmp_path.glob("2026*.json"))).read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    bad_version.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_runs(tmp_path)
    assert len(loaded.runs) == 1
    skipped_names = {s.path.name for s in loaded.skipped}
    assert skipped_names == {"broken.json", "bad-version.json"}
    assert all(s.reason for s in loaded.skipped)


def test_load_runs_on_missing_dir_returns_empty(tmp_path) -> None:
    loaded = load_runs(tmp_path / "does-not-exist")
    assert loaded.runs == []
    assert loaded.skipped == []
