"""`sqlproof scale report` over saved scale runs (scale follow-up 5)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlproof.cli import main
from sqlproof.scale.artifact import save_run
from sqlproof.scale.fit import fit_exponent
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.report import load_report, render_html, render_text
from sqlproof.scale.result import ScaleResult


def _result(function: str, power: float, *, ms: float = 1.0, temp_at: int | None = None):
    points = [
        ProbePoint(
            factor=f,
            total_rows=f * 100,
            work_blocks=int(100 * f**power) + 4,
            peak_memory_kb=0,
            temp_blocks=1 if temp_at is not None and f >= temp_at else 0,
            plan_hash="a",
            exec_ms=ms * f**power,
        )
        for f in (1, 2, 4, 8, 16)
    ]
    return ScaleResult(
        points=points,
        regimes=[fit_exponent(points, 4)],
        plan_flips=[],
        truncated=False,
        function=function,
        sizes={"t": 100},
        baseline=4,
        seed=0,
    )


def _save(runs: Path, result: ScaleResult, day: int, **kw) -> Path:
    return save_run(
        result,
        runs,
        started_at=datetime(2026, 10, day, tzinfo=UTC),
        git_sha=f"sha{day}",
        git_dirty=False,
        **kw,
    )


def test_runs_group_by_function_oldest_first_and_flag_regressions(tmp_path: Path) -> None:
    _save(tmp_path, _result("public.totals", 1.0), 3)
    _save(tmp_path, _result("public.totals", 1.0), 1)
    _save(tmp_path, _result("public.totals", 2.0), 5)
    _save(tmp_path, _result("public.lookup", 0.0), 2)
    report = load_report(tmp_path)
    assert [h.function for h in report.functions] == ["public.lookup", "public.totals"]
    totals = report.functions[1].runs
    assert [r.git_sha for r in totals] == ["sha1", "sha3", "sha5"]
    assert [round(r.exponent or 0, 1) for r in totals] == [1.0, 1.0, 2.0]
    assert [r.regressed for r in totals] == [False, False, True]


def test_a_refused_exponent_is_shown_as_refused_with_the_reason(tmp_path: Path) -> None:
    # Work grows ~n while the clock grows ~n^2: the CPU guard refuses it.
    cpu = _result("public.cpu", 1.0)
    timed = [replace(p, exec_ms=2.0 * p.factor**2) for p in cpu.points]
    _save(tmp_path, replace(cpu, points=timed), 1)
    [history] = load_report(tmp_path).functions
    assert history.latest.exponent is None
    assert history.latest.refused is not None and "CPU cost" in history.latest.refused
    assert "public.cpu: refused" in render_text(load_report(tmp_path))


def test_unreadable_files_are_skipped_not_raised(tmp_path: Path) -> None:
    _save(tmp_path, _result("public.totals", 1.0), 1)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "future.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    report = load_report(tmp_path)
    assert len(report.functions) == 1
    reasons = {s.path.name: s.reason for s in report.skipped}
    assert "JSONDecodeError" in reasons["broken.json"]
    assert "schema_version 99" in reasons["future.json"]


def test_html_names_every_function_and_escapes_them(tmp_path: Path) -> None:
    _save(tmp_path, _result("public.totals", 1.0, temp_at=8), 1)
    _save(tmp_path, _result("public.totals", 1.0, temp_at=8), 2)
    page = render_html(load_report(tmp_path))
    assert "<title>SqlProof Scale Report</title>" in page
    assert "public.totals" in page
    assert "<svg" in page  # two fitted runs draw a trend
    assert "800" in page  # the spill point, in rows


def test_an_empty_directory_says_how_to_make_runs(tmp_path: Path) -> None:
    assert "No scale runs found" in render_html(load_report(tmp_path / "missing"))
    assert render_text(load_report(tmp_path)).startswith("No scale runs found")


def test_cli_writes_html_and_prints_text(tmp_path: Path, capsys) -> None:
    runs = tmp_path / "runs"
    _save(runs, _result("public.totals", 1.0), 1)
    out = tmp_path / "out" / "scale.html"
    assert main(["scale", "report", "--runs-dir", str(runs), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "1 function(s)" in capsys.readouterr().out
    assert main(["scale", "report", "--runs-dir", str(runs), "--format", "text"]) == 0
    assert "public.totals: 1.00" in capsys.readouterr().out


def test_the_latest_runs_advisor_hits_show_in_text_and_html(tmp_path: Path) -> None:
    """Scale follow-up 6: the report shows the ranked index suggestions."""
    from sqlproof.scale.advisors import AdvisorHit

    hit = AdvisorHit(
        rank=1, relation="public.invoices",
        index_statement="CREATE INDEX ON public.invoices USING btree (customer_id)",
        statement="SELECT count(*) FROM public.invoices WHERE customer_id < ($1::bigint)",
        startup_cost_before=0.0, startup_cost_after=0.0,
        total_cost_before=1887.26, total_cost_after=271.72,
    )
    _save(tmp_path, replace(_result("public.count_for", 1.0), advisors=True,
                            advisor_hits=[hit]), 2)
    report = load_report(tmp_path)
    assert report.functions[0].latest.advisor_hits == (hit,)
    text = render_text(report)
    assert (
        "  advisor #1: CREATE INDEX ON public.invoices USING btree (customer_id)"
        "  (est. cost 1,887.26 -> 271.72)"
    ) in text
    page = render_html(report)
    assert "Index suggestions (latest run)" in page
    assert "customer_id &lt; ($1::bigint)" in page


def test_a_run_saved_before_advisors_existed_still_reads(tmp_path: Path) -> None:
    path = _save(tmp_path, _result("public.old", 1.0), 1)
    payload = json.loads(path.read_text())
    for key in ("advisors", "advisor_hits", "advisor_notes"):
        del payload[key]
    path.write_text(json.dumps(payload))
    report = load_report(tmp_path)
    assert report.skipped == []
    assert report.functions[0].latest.advisor_hits == ()
    assert "Index suggestions" not in render_html(report)
