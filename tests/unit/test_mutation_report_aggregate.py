from __future__ import annotations

import json

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.report.aggregate import build_report, load_runs
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


def _outcome(mutant_id, target, status):  # type: ignore[no-untyped-def]
    return MutantOutcome(
        mutant_id=mutant_id,
        target=target,
        description=f"{target}: mutate {mutant_id}",
        status=status,
        pytest_exit_code=1 if status in ("killed", "unexpected_kill") else 0,
        hypothesis_seed=42,
        detail=None,
        duration_s=0.5,
    )


def test_build_report_computes_score_excluding_errors_and_expected(tmp_path) -> None:
    _write(
        tmp_path,
        "aaaaaaaa",
        "2026-06-11T10:00:00Z",
        outcomes=[
            _outcome("k1", "f", "killed"),
            _outcome("s1", "f", "survived"),
            _outcome("e1", "f", "error"),
            _outcome("x1", "f", "expected_survivor"),
        ],
    )
    report = build_report(load_runs(tmp_path))
    run = report.runs[0]
    assert run.killed == 1
    assert run.survived == 1
    assert run.errored == 1
    assert run.score == 0.5  # 1 / (1 + 1)


def test_build_report_score_is_none_when_denominator_zero(tmp_path) -> None:
    _write(
        tmp_path,
        "aaaaaaaa",
        "2026-06-11T10:00:00Z",
        outcomes=[_outcome("e1", "f", "error"), _outcome("x1", "f", "expected_survivor")],
    )
    report = build_report(load_runs(tmp_path))
    assert report.runs[0].score is None


def test_build_report_classifies_new_vs_known_survivors(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("s1", "f", "survived")])
    _write(
        tmp_path,
        "bbbbbbbb",
        "2026-06-12T10:00:00Z",
        outcomes=[_outcome("s1", "f", "survived"), _outcome("s2", "f", "survived")],
    )
    report = build_report(load_runs(tmp_path))
    by_id = {s.mutant_id: s for s in report.latest_survivors}
    assert by_id["s1"].is_new is False  # seen in the earlier run
    assert by_id["s2"].is_new is True   # first appearance


def test_build_report_repro_command_includes_args_and_seed(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("s1", "f", "survived")])
    report = build_report(load_runs(tmp_path))
    cmd = report.latest_survivors[0].repro_command
    assert "pytest tests/" in cmd
    assert "--hypothesis-seed=42" in cmd
    assert "s1" in cmd


def test_build_report_flags_schema_drift(tmp_path) -> None:
    p1 = _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    p2 = _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    payload = json.loads(p2.read_text(encoding="utf-8"))
    payload["schema_fingerprint"] = "sha256:DIFFERENT"
    p2.write_text(json.dumps(payload), encoding="utf-8")
    report = build_report(load_runs(tmp_path))
    assert report.runs[0].schema_changed is False  # first run
    assert report.runs[1].schema_changed is True   # fingerprint changed


def test_build_report_per_target_history(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z", outcomes=[_outcome("k1", "f", "survived")])
    report = build_report(load_runs(tmp_path))
    target = next(t for t in report.targets if t.target == "f")
    assert [point.score for point in target.history] == [1.0, 0.0]
    assert target.latest_score == 0.0
