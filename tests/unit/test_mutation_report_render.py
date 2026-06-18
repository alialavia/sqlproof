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
