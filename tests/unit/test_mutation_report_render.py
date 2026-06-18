from __future__ import annotations

import re

from sqlproof.mutation.report.aggregate import (
    LoadResult,
    ReportData,
    RunSummary,
    SkippedFile,
    SurvivorEntry,
    TargetSummary,
    TrendPoint,
    build_report,
)
from sqlproof.mutation.report.render import render_html


def _report() -> ReportData:
    return ReportData(
        runs=[
            RunSummary(
                run_id="aaaaaaaa",
                started_at="2026-06-11T10:00:00Z",
                git_sha="abc1234",
                git_dirty=False,
                duration_s=12.0,
                killed=3,
                survived=1,
                errored=0,
                score=0.75,
                schema_fingerprint="sha256:s1",
                schema_changed=False,
            )
        ],
        targets=[
            TargetSummary(
                target="billing.f",
                latest_score=0.75,
                mutant_count=4,
                survivor_count=1,
                history=[TrendPoint(started_at="2026-06-11T10:00:00Z", score=0.75)],
            )
        ],
        latest_survivors=[
            SurvivorEntry(
                mutant_id="s1",
                target="billing.f",
                description="drop FILTER (WHERE active)",
                repro_command="pytest tests/ --hypothesis-seed=42 # mutant s1",
                is_new=True,
            )
        ],
        skipped=[],
    )


def test_render_returns_self_contained_html() -> None:
    html = render_html(_report())
    assert html.lstrip().lower().startswith("<!doctype html")
    # No external resources: no http(s) src/href references.
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)


def test_render_embeds_survivor_and_repro() -> None:
    html = render_html(_report())
    assert "billing.f" in html
    assert "pytest tests/ --hypothesis-seed=42" in html
    assert "NEW" in html  # new-survivor badge


def test_render_escapes_html_in_descriptions() -> None:
    report = _report()
    report.latest_survivors[0] = SurvivorEntry(
        mutant_id="s1",
        target="t",
        description="x < y AND <script>alert(1)</script>",
        repro_command="pytest",
        is_new=False,
    )
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_empty_report_says_no_runs() -> None:
    empty = build_report(LoadResult(runs=[], skipped=[]))
    html = render_html(empty)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "no runs found" in html.lower()


def test_render_lists_skipped_files() -> None:
    report = _report()
    from pathlib import Path

    report.skipped.append(SkippedFile(path=Path("broken.json"), reason="JSONDecodeError: x"))
    html = render_html(report)
    assert "broken.json" in html


def test_render_runs_present_but_none_scored() -> None:
    report = ReportData(
        runs=[
            RunSummary(
                run_id="a",
                started_at="2026-06-11T10:00:00Z",
                git_sha=None,
                git_dirty=False,
                duration_s=1.0,
                killed=0,
                survived=0,
                errored=1,
                score=None,
                schema_fingerprint=None,
                schema_changed=False,
            )
        ],
        targets=[],
        latest_survivors=[],
        skipped=[],
    )
    html = render_html(report)
    assert "No scored runs yet" in html


def test_render_surfaces_errored_count_in_run_log() -> None:
    report = ReportData(
        runs=[
            RunSummary(
                run_id="a",
                started_at="2026-06-11T10:00:00Z",
                git_sha="abc1234",
                git_dirty=False,
                duration_s=1.0,
                killed=2,
                survived=0,
                errored=5,
                score=1.0,
                schema_fingerprint="sha256:s1",
                schema_changed=False,
            )
        ],
        targets=[],
        latest_survivors=[],
        skipped=[],
    )
    html = render_html(report)
    # The errored count must appear in a cell flagged with the `err` class.
    assert re.search(r'class=err[^>]*>5<', html), "errored count not in err-styled cell"
    assert "Errors" in html, "Errors column header missing"


def test_render_drift_line_stays_within_plot_with_unscored_runs() -> None:
    import re

    def run(run_id, started_at, score, schema_changed):  # type: ignore[no-untyped-def]
        return RunSummary(
            run_id=run_id,
            started_at=started_at,
            git_sha="abc1234",
            git_dirty=False,
            duration_s=1.0,
            killed=1 if score else 0,
            survived=0,
            errored=0 if score else 1,
            score=score,
            schema_fingerprint="sha256:x",
            schema_changed=schema_changed,
        )

    report = ReportData(
        runs=[
            run("a", "2026-06-11T10:00:00Z", 1.0, False),
            run("b", "2026-06-12T10:00:00Z", None, False),   # unscored — not a chart point
            run("c", "2026-06-13T10:00:00Z", 0.5, True),     # scored + schema changed
        ],
        targets=[],
        latest_survivors=[],
        skipped=[],
    )
    html = render_html(report)
    # There are 2 scored points → x spans [30, 690] in a 720-wide chart.
    # The drift line's x must be the SECOND point's x (690), not an out-of-bounds value.
    xs = [float(m) for m in re.findall(r'<line x1="([\d.]+)"', html)]
    assert xs, "expected a drift line"
    assert all(x <= 720 for x in xs), f"drift x out of bounds: {xs}"
    assert any(abs(x - 690.0) < 1.0 for x in xs), f"drift not at 2nd point: {xs}"
