"""`sqlproof scale report`: per-function history over saved scale runs.

Reads the artifacts `scale_analysis` writes (`.sqlproof/scale-runs/*.json`)
and renders, per function, the exponent over time, with each run's fit,
measured range, spill and plan changes. The assertion API stays the CI
gate; this is for reading the trend, as `sqlproof mutation report` is
for mutation runs.

Each run is re-read into a `ScaleResult`, so the report applies the same
rules a test would: an exponent the fit or the CPU guard refuses shows as
refused, with the reason, never as a number.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.mutation.report.render import _CSS  # pyright: ignore[reportPrivateUsage]
from sqlproof.scale.advisors import AdvisorHit
from sqlproof.scale.artifact import SCHEMA_VERSION
from sqlproof.scale.fit import FitResult, PlanFlip
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.result import ScaleResult

REGRESSION = 0.3
"""An exponent this much above the function's previous run is flagged."""


@dataclass(frozen=True, slots=True)
class Skipped:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ScaleRun:
    path: Path
    started_at: str
    function: str
    git_sha: str | None
    git_dirty: bool
    schema_fingerprint: str | None
    exponent: float | None
    refused: str | None
    """Why no exponent is claimed, when none is."""
    slope_stderr: float | None
    measured_rows: int
    truncated: bool
    spill_point_rows: int | None
    plan_flips: int
    distribution: str
    regressed: bool = False
    advisor_hits: tuple[AdvisorHit, ...] = ()
    """Ranked index_advisor suggestions, when the run asked for them."""


@dataclass(frozen=True, slots=True)
class FunctionHistory:
    function: str
    runs: list[ScaleRun] = field(default_factory=lambda: [])

    @property
    def latest(self) -> ScaleRun:
        return self.runs[-1]


@dataclass(frozen=True, slots=True)
class ScaleReport:
    functions: list[FunctionHistory]
    skipped: list[Skipped]


def load_report(runs_dir: Path) -> ScaleReport:
    """Every run in `runs_dir`, grouped by function, oldest first. A file
    that is not a readable scale artifact is skipped with its reason, never
    raised: one bad file must not hide the rest."""
    runs: list[ScaleRun] = []
    skipped: list[Skipped] = []
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            try:
                runs.append(_read(path))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                skipped.append(Skipped(path, f"{type(exc).__name__}: {exc}"))
    by_function: dict[str, list[ScaleRun]] = {}
    for run in sorted(runs, key=lambda r: (r.started_at, r.path.name)):
        history = by_function.setdefault(run.function, [])
        previous = next((r.exponent for r in reversed(history) if r.exponent is not None), None)
        regressed = (
            run.exponent is not None
            and previous is not None
            and run.exponent - previous > REGRESSION
        )
        history.append(replace(run, regressed=regressed))
    functions = [FunctionHistory(name, runs) for name, runs in sorted(by_function.items())]
    return ScaleReport(functions, skipped)


def _read(path: Path) -> ScaleRun:
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = "not a JSON object"
        raise ValueError(msg)
    payload: dict[str, Any] = loaded  # pyright: ignore[reportUnknownVariableType]
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        msg = f"schema_version {version!r}; this sqlproof reads {SCHEMA_VERSION}"
        raise ValueError(msg)
    result = _result(payload)
    exponent: float | None
    refused: str | None
    try:
        exponent, refused = result.exponent, None
    except SqlProofScaleError as exc:
        exponent, refused = None, str(exc)
    final = result.regimes[-1] if result.regimes else None
    return ScaleRun(
        path=path,
        started_at=str(payload["started_at"]),
        function=result.function,
        git_sha=payload.get("git_sha"),
        git_dirty=bool(payload.get("git_dirty")),
        schema_fingerprint=payload.get("schema_fingerprint"),
        exponent=exponent,
        refused=refused,
        slope_stderr=None if final is None else final.slope_stderr,
        measured_rows=max((p.total_rows for p in result.points), default=0),
        truncated=result.truncated,
        spill_point_rows=result.spill_point_rows,
        plan_flips=len(result.plan_flips),
        distribution=str(payload.get("distribution") or "uniform"),
        advisor_hits=tuple(AdvisorHit(**hit) for hit in payload.get("advisor_hits") or ()),
    )


def _result(payload: Mapping[str, Any]) -> ScaleResult:
    points: list[ProbePoint] = []
    for raw in payload["points"]:
        point: dict[str, Any] = dict(raw)
        point["args"] = tuple(point.get("args", ()))
        points.append(ProbePoint(**point))
    return ScaleResult(
        points=points,
        regimes=[FitResult(**regime) for regime in payload["regimes"]],
        plan_flips=[PlanFlip(**flip) for flip in payload["plan_flips"]],
        truncated=bool(payload["truncated"]),
        function=str(payload["function"]),
        sizes=dict(payload["sizes"]),
        baseline=payload.get("baseline"),
        seed=payload.get("seed"),
    )


# -- text -----------------------------------------------------------------------


def render_text(report: ScaleReport) -> str:
    """One line per function: its latest run, and the trend."""
    if not report.functions:
        return "No scale runs found. Run scale_analysis(...) first.\n"
    lines: list[str] = []
    for history in report.functions:
        latest = history.latest
        trend = " -> ".join(_exponent_text(r) for r in history.runs[-5:])
        flag = "  REGRESSED" if latest.regressed else ""
        lines.append(f"{history.function}: {_exponent_text(latest)}{flag}  (history: {trend})")
        if latest.refused:
            lines.append(f"  refused: {latest.refused}")
        if latest.spill_point_rows is not None:
            lines.append(f"  spills at {latest.spill_point_rows:,} rows")
        for hit in latest.advisor_hits[:3]:
            lines.append(f"  advisor #{hit.rank}: {hit.index_statement}  ({_cost_text(hit)})")
    for skipped in report.skipped:
        lines.append(f"skipped {skipped.path.name}: {skipped.reason}")
    return "\n".join(lines) + "\n"


def _cost_text(hit: AdvisorHit) -> str:
    return f"est. cost {hit.total_cost_before:,.2f} -> {hit.total_cost_after:,.2f}"


def _exponent_text(run: ScaleRun) -> str:
    return "refused" if run.exponent is None else f"{run.exponent:.2f}"


# -- html -----------------------------------------------------------------------


def render_html(report: ScaleReport) -> str:
    if not report.functions:
        body = (
            '<div class="empty">No scale runs found. Run '
            "<code>scale_analysis(...)</code> first; it writes to "
            "<code>.sqlproof/scale-runs/</code>.</div>"
        )
    else:
        body = "\n".join(
            [_summary(report), *(_function_section(h) for h in report.functions), _skipped(report)]
        )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset=utf-8>'
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SqlProof Scale Report</title>"
        f"<style>{_CSS}</style></head><body>"
        "<header class=hdr><span class=logo>SqlProof</span>"
        "<span class=label>// scale report</span></header>"
        f"{body}</body></html>"
    )


def _summary(report: ScaleReport) -> str:
    rows = "".join(
        f"<tr><td><a href='#{_anchor(h.function)}'>{html.escape(h.function)}</a></td>"
        f"<td>{_exponent_cell(h.latest)}</td><td>{len(h.runs)}</td>"
        f"<td>{html.escape(h.latest.started_at)}</td></tr>"
        for h in report.functions
    )
    return (
        "<h2>Functions (latest run)</h2><table>"
        "<tr><th>Function</th><th>Exponent</th><th>Runs</th><th>Latest</th></tr>"
        f"{rows}</table>"
    )


def _function_section(history: FunctionHistory) -> str:
    rows: list[str] = []
    for run in reversed(history.runs):
        sha = html.escape(run.git_sha or "—") + ("*" if run.git_dirty else "")
        spill = "—" if run.spill_point_rows is None else f"{run.spill_point_rows:,}"
        stderr = "—" if run.slope_stderr is None else f"±{run.slope_stderr:.3f}"
        rows.append(
            f"<tr><td>{html.escape(run.started_at)}</td><td>{sha}</td>"
            f"<td>{_exponent_cell(run)}</td><td>{stderr}</td>"
            f"<td>{run.measured_rows:,}{' (truncated)' if run.truncated else ''}</td>"
            f"<td>{spill}</td><td>{run.plan_flips}</td>"
            f"<td>{html.escape(run.distribution)}</td></tr>"
        )
    refused = history.latest.refused
    note = f"<p class=muted>{html.escape(refused)}</p>" if refused else ""
    return (
        f"<h2 id='{_anchor(history.function)}'>{html.escape(history.function)}</h2>"
        f"{_chart(history.runs)}{note}<table>"
        "<tr><th>Started</th><th>Commit</th><th>Exponent</th><th>Std. error</th>"
        "<th>Rows measured</th><th>Spill at</th><th>Plan flips</th><th>Data</th></tr>"
        f"{''.join(rows)}</table>{_advisor_table(history.latest)}"
    )


def _advisor_table(run: ScaleRun) -> str:
    """The latest run's index_advisor suggestions, best first."""
    if not run.advisor_hits:
        return ""
    rows = "".join(
        f"<tr><td>{hit.rank}</td><td><code>{html.escape(hit.index_statement)}</code></td>"
        f"<td>{hit.total_cost_before:,.2f}</td><td>{hit.total_cost_after:,.2f}</td>"
        f"<td><code>{html.escape(hit.statement)}</code></td></tr>"
        for hit in run.advisor_hits
    )
    return (
        "<h3>Index suggestions (latest run)</h3><table>"
        "<tr><th>Rank</th><th>Index</th><th>Est. cost before</th>"
        "<th>Est. cost after</th><th>From statement</th></tr>"
        f"{rows}</table>"
    )


def _exponent_cell(run: ScaleRun) -> str:
    if run.exponent is None:
        return "<span class=err>refused</span>"
    badge = " <span class=badge-new>REGRESSED</span>" if run.regressed else ""
    return f"{run.exponent:.2f}{badge}"


def _chart(runs: Sequence[ScaleRun]) -> str:
    fitted = [(i, r.exponent) for i, r in enumerate(runs) if r.exponent is not None]
    if len(fitted) < 2:
        return ""
    width, height, pad = 720, 160, 28
    top = max(2.5, max(e for _, e in fitted) + 0.25)
    n = len(runs)

    def x(i: int) -> float:
        return pad + i * (width - 2 * pad) / max(n - 1, 1)

    def y(value: float) -> float:
        return height - pad - max(value, 0.0) / top * (height - 2 * pad)

    grid = "".join(
        f'<line x1="{pad}" y1="{y(v):.1f}" x2="{width - pad}" y2="{y(v):.1f}"'
        ' stroke="var(--border)"/>'
        f'<text x="2" y="{y(v) + 3:.1f}" fill="var(--faint)" font-size="10">{v:g}</text>'
        for v in (0, 1, 2)
    )
    line = " ".join(f"{x(i):.1f},{y(e):.1f}" for i, e in fitted)
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(e):.1f}" r="3.5" '
        f'fill="{"var(--warn)" if runs[i].regressed else "var(--accent)"}"/>'
        for i, e in fitted
    )
    return (
        f"<div class=panel><svg viewBox='0 0 {width} {height}' width='100%'>{grid}"
        f"<polyline fill=none stroke='var(--accent)' stroke-width='2' points='{line}'/>"
        f"{dots}</svg></div>"
    )


def _skipped(report: ScaleReport) -> str:
    if not report.skipped:
        return ""
    items = "".join(
        f"<li><code>{html.escape(s.path.name)}</code> — {html.escape(s.reason)}</li>"
        for s in report.skipped
    )
    return f"<h2>Skipped artifacts</h2><ul>{items}</ul>"


def _anchor(function: str) -> str:
    return "fn-" + "".join(c if c.isalnum() else "-" for c in function)
