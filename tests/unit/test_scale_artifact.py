"""Run artifacts.

Mirrors the mutation-run artifact conventions (schema_version, git sha,
schema fingerprint) so both feed one ingester later. Writing it is a
side effect of a scale run, not its point -- the assertion is the point.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

import sqlproof
from sqlproof.scale.artifact import save_run
from sqlproof.scale.fit import FitResult, PlanFlip, fit_exponent, segment_by_plan
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.result import ScaleResult


def _result():
    return ScaleResult(
        points=[
            ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                       peak_memory_kb=0, temp_blocks=0, plan_hash="a",
                       exec_ms=float(f), args=(7,))
            for f in (1, 2, 4, 8, 16)
        ],
        regimes=[FitResult(1.0, 0.999, None, 1, 16, "a")],
        plan_flips=[],
        truncated=False,
        function="billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000},
    )


def _flipped_result():
    """Points span two plan shapes; the second regime is refused, and a
    spill shows up starting at the second shape's first point."""
    points = [
        ProbePoint(factor=1, total_rows=1000, work_blocks=100, peak_memory_kb=0,
                   temp_blocks=0, plan_hash="a", exec_ms=1.0, args=(7,)),
        ProbePoint(factor=2, total_rows=2000, work_blocks=200, peak_memory_kb=0,
                   temp_blocks=0, plan_hash="a", exec_ms=2.0, args=(7,)),
        ProbePoint(factor=4, total_rows=4000, work_blocks=900, peak_memory_kb=512,
                   temp_blocks=64, plan_hash="b", exec_ms=9.0, args=(7,)),
        ProbePoint(factor=8, total_rows=8000, work_blocks=1800, peak_memory_kb=1024,
                   temp_blocks=128, plan_hash="b", exec_ms=18.0, args=(7,)),
    ]
    return ScaleResult(
        points=points,
        regimes=[
            FitResult(1.0, 0.999, None, 1, 2, "a"),
            FitResult(
                None, None,
                "plan changed at factor 4; refusing to fit across a discontinuity",
                4, 8, "b",
            ),
        ],
        plan_flips=[PlanFlip(at_factor=4, from_hash="a", to_hash="b")],
        truncated=False,
        function="billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000},
    )


def test_artifact_round_trips_the_measured_points(tmp_path):
    path = save_run(_result(), tmp_path)
    data = json.loads(path.read_text())
    assert data["function"] == "billing.compute_invoice"
    assert len(data["points"]) == 5
    assert data["points"][0]["factor"] == 1


def test_artifact_records_the_exponent_and_fit_quality(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["regimes"][0]["exponent"] == 1.0
    assert data["regimes"][0]["r_squared"] == 0.999


def test_artifact_records_resolved_arguments_per_point(tmp_path):
    """Without these a surprising result is unreproducible."""
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["points"][0]["args"] == [7]


def test_artifact_labels_the_worst_case_argument_policy(tmp_path):
    """Results describe worst-case behaviour when `heaviest` is used.
    Unlabelled, someone reads the exponent as a median."""
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert "worst-case" in data["argument_policy"]


def test_artifact_carries_a_schema_version(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["schema_version"] == 1


def test_missing_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "runs"
    path = save_run(_result(), target)
    assert path.exists()


def test_artifact_records_plan_flips_exactly(tmp_path):
    data = json.loads(save_run(_flipped_result(), tmp_path).read_text())
    assert data["plan_flips"][0] == {"at_factor": 4, "from_hash": "a", "to_hash": "b"}


def test_artifact_records_a_refused_final_regime(tmp_path):
    data = json.loads(save_run(_flipped_result(), tmp_path).read_text())
    assert data["regimes"][-1]["exponent"] is None
    assert data["regimes"][-1]["r_squared"] is None
    assert data["regimes"][-1]["reason"] == (
        "plan changed at factor 4; refusing to fit across a discontinuity"
    )


def test_artifact_records_the_spill_point_in_both_units(tmp_path):
    data = json.loads(save_run(_flipped_result(), tmp_path).read_text())
    assert data["spill_point_rows"] == 4000
    assert data["factor_at_spill"] == 4


def test_artifact_spill_fields_are_null_without_a_spill(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["spill_point_rows"] is None
    assert data["factor_at_spill"] is None


def test_two_runs_with_the_same_started_at_do_not_collide(tmp_path):
    started = datetime(2026, 1, 1, tzinfo=UTC)
    path1 = save_run(_result(), tmp_path, started_at=started)
    path2 = save_run(_result(), tmp_path, started_at=started)
    assert path1 != path2
    assert json.loads(path1.read_text())["function"] == "billing.compute_invoice"
    assert json.loads(path2.read_text())["function"] == "billing.compute_invoice"


def test_artifact_envelope_fields(tmp_path):
    started = datetime(2026, 1, 1, 12, 30, 45, tzinfo=UTC)
    path = save_run(
        _result(), tmp_path, started_at=started, duration_s=1.5,
        schema_fingerprint="fp-abc",
    )
    data = json.loads(path.read_text())
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", data["started_at"])
    assert data["started_at"] == "2026-01-01T12:30:45Z"
    assert data["duration_s"] == 1.5
    assert data["sqlproof_version"] == sqlproof.__version__
    assert "git_sha" in data
    assert "git_dirty" in data
    assert data["schema_fingerprint"] == "fp-abc"
    assert data["run_id"] in path.name


def test_artifact_started_at_defaults_to_now_when_not_given(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", data["started_at"])
    assert data["duration_s"] is None
    assert data["schema_fingerprint"] is None


def _fitted_result():
    """Points exactly quadratic above a fixed 100-block baseline, fitted
    the way `run_sweep` fits them, carrying the sweep parameters
    `run_sweep` records."""
    points = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=100 + 50 * f * f,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a",
                   exec_ms=float(f), args=(7,))
        for f in (1, 2, 4, 8, 16)
    ]
    segments, flips = segment_by_plan(points)
    return ScaleResult(
        points=points,
        regimes=[fit_exponent(segment, 100) for segment in segments],
        plan_flips=flips,
        truncated=False,
        function="billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000},
        baseline=100,
        seed=7,
        max_factor=32,
        min_points=5,
        probe_timeout_s=30.0,
    )


def test_the_artifact_re_fits_to_the_exponent_it_records(tmp_path):
    """Ruling AQ: the spec promises a cloud tier that re-uses fit.py on
    artifacts, which needs the points AND the calibrated baseline.
    Rebuilt from the JSON alone, the fit must reproduce the recorded
    exponent -- and the baseline must be load-bearing, or this would
    pass without it being recorded at all."""
    data = json.loads(save_run(_fitted_result(), tmp_path).read_text())
    points = [ProbePoint(**{**p, "args": tuple(p["args"])}) for p in data["points"]]
    segments, _flips = segment_by_plan(points)
    refit = fit_exponent(segments[-1], data["baseline"])
    assert refit.exponent == data["regimes"][-1]["exponent"] == 2.0
    assert fit_exponent(segments[-1], 0).exponent != data["regimes"][-1]["exponent"]


def test_the_artifact_records_how_the_sweep_was_run(tmp_path):
    data = json.loads(save_run(_fitted_result(), tmp_path).read_text())
    assert data["baseline"] == 100
    assert data["seed"] == 7
    assert data["max_factor"] == 32
    assert data["min_points"] == 5
    assert data["probe_timeout_s"] == 30.0


@pytest.mark.parametrize(("sha", "dirty"), [("abc1234", True), (None, False)])
def test_given_git_state_is_recorded_without_capturing_again(tmp_path, monkeypatch, sha, dirty):
    """`scale_analysis` captures git state BEFORE the sweep and passes it
    in. save_run must record exactly that -- including (None, False), a
    run outside any git repository -- rather than re-capture at save
    time."""

    def must_not_capture():
        raise AssertionError("save_run re-captured git state it was given")

    monkeypatch.setattr("sqlproof.scale.artifact.capture_git_info", must_not_capture)
    path = save_run(_result(), tmp_path, git_sha=sha, git_dirty=dirty)
    data = json.loads(path.read_text())
    assert data["git_sha"] == sha
    assert data["git_dirty"] is dirty


def test_git_state_is_captured_at_save_time_when_not_given(tmp_path, monkeypatch):
    monkeypatch.setattr("sqlproof.scale.artifact.capture_git_info", lambda: ("fedcba9", False))
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["git_sha"] == "fedcba9"
    assert data["git_dirty"] is False
