from __future__ import annotations

import html
import json
from dataclasses import asdict

from sqlproof.mutation.report.aggregate import ReportData

_CSS = """
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#7d8590; --line:#1a7f37;
          --bad:#cf222e; --new:#bf3989; --panel:#161b22; --border:#30363d; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  h1 { font-size:20px; } h2 { font-size:15px; margin:28px 0 8px; }
  .muted { color:var(--muted); }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; }
  .badge-new { background:var(--new); color:#fff; border-radius:10px;
               padding:1px 8px; font-size:11px; font-weight:700; }
  .panel { background:var(--panel); border:1px solid var(--border);
           border-radius:8px; padding:16px; }
  code { background:#0d1117; border:1px solid var(--border); border-radius:4px;
         padding:2px 6px; font-size:12px; }
  .drift { color:#d29922; }
  .empty { text-align:center; color:var(--muted); padding:60px 0; }
"""


def render_html(report: ReportData) -> str:
    if not report.runs:
        body = '<div class="empty">No runs found. Run mutations with '
        body += "<code>run_mutation_tests(..., artifact_dir=...)</code> first.</div>"
        return _page(body, report)
    sections = [
        _chart_section(report),
        _targets_section(report),
        _survivors_section(report),
        _runlog_section(report),
        _skipped_section(report),
    ]
    return _page("\n".join(sections), report)


def _page(body: str, report: ReportData) -> str:
    blob = json.dumps(
        {
            "runs": [asdict(r) for r in report.runs],
            "targets": [asdict(t) for t in report.targets],
        }
    )
    # Escape '</' inside the JSON so a "</script>" in data can't end the tag.
    blob = blob.replace("</", "<\\/")
    return (
        "<!doctype html>\n<html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SqlProof Mutation Report</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>SqlProof — Mutation Report</h1>"
        f"{body}"
        f"<script id=report-data type=application/json>{blob}</script>"
        "</body></html>"
    )


def _chart_section(report: ReportData) -> str:
    points = [(r.started_at, r.score) for r in report.runs if r.score is not None]
    if not points:
        return "<h2>Mutation score over time</h2><p class=muted>No scored runs yet.</p>"
    width, height, pad = 720, 220, 30
    n = len(points)

    def x(i: int) -> float:
        return pad if n == 1 else pad + i * (width - 2 * pad) / (n - 1)

    def y(score: float) -> float:
        return height - pad - score * (height - 2 * pad)

    coords = " ".join(f"{x(i):.1f},{y(s):.1f}" for i, (_, s) in enumerate(points))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(s):.1f}" r="3" fill="var(--line)"/>'
        for i, (_, s) in enumerate(points)
    )
    drift = "".join(
        f'<line x1="{x(i):.1f}" y1="{pad}" x2="{x(i):.1f}" y2="{height - pad}"'
        ' stroke="var(--drift,#d29922)" stroke-dasharray="3 3"/>'
        for i, r in enumerate(report.runs)
        if r.schema_changed and r.score is not None
    )
    latest = f"{points[-1][1] * 100:.0f}%"
    return (
        "<h2>Mutation score over time</h2>"
        f"<div class=panel><div style='font-size:32px;font-weight:700'>{latest}"
        "<span class=muted style='font-size:14px'> latest</span></div>"
        f"<svg viewBox='0 0 {width} {height}' width='100%'>"
        f"<polyline fill=none stroke='var(--line)' stroke-width=2 points='{coords}'/>"
        f"{dots}{drift}</svg>"
        "<p class=muted>Dashed gold lines mark runs where the schema fingerprint changed.</p>"
        "</div>"
    )


def _targets_section(report: ReportData) -> str:
    rows: list[str] = []
    for t in report.targets:
        score = "—" if t.latest_score is None else f"{t.latest_score * 100:.0f}%"
        rows.append(
            f"<tr><td>{html.escape(t.target)}</td><td>{score}</td>"
            f"<td>{t.mutant_count}</td><td>{t.survivor_count}</td></tr>"
        )
    return (
        "<h2>Per-target (latest run)</h2><table>"
        "<tr><th>Target</th><th>Score</th><th>Mutants</th><th>Survivors</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _survivors_section(report: ReportData) -> str:
    if not report.latest_survivors:
        return "<h2>Survivors (latest run)</h2><p class=muted>None — all mutants killed.</p>"
    rows: list[str] = []
    for s in report.latest_survivors:
        badge = "<span class=badge-new>NEW</span> " if s.is_new else ""
        rows.append(
            f"<tr><td>{badge}{html.escape(s.target)}</td>"
            f"<td>{html.escape(s.description)}</td>"
            f"<td><code>{html.escape(s.repro_command)}</code></td></tr>"
        )
    return (
        "<h2>Survivors (latest run)</h2><table>"
        "<tr><th>Target</th><th>Mutation</th><th>Reproduce</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _runlog_section(report: ReportData) -> str:
    rows: list[str] = []
    for r in report.runs:
        score = "—" if r.score is None else f"{r.score * 100:.0f}%"
        sha = html.escape(r.git_sha or "—") + ("*" if r.git_dirty else "")
        drift = " <span class=drift>(schema changed)</span>" if r.schema_changed else ""
        rows.append(
            f"<tr><td>{html.escape(r.started_at)}{drift}</td><td>{sha}</td>"
            f"<td>{score}</td><td>{r.duration_s:.1f}s</td></tr>"
        )
    return (
        "<h2>Run log</h2><table>"
        "<tr><th>Started</th><th>Commit</th><th>Score</th><th>Duration</th></tr>"
        f"{''.join(reversed(rows))}</table>"
    )


def _skipped_section(report: ReportData) -> str:
    if not report.skipped:
        return ""
    items = "".join(
        f"<li><code>{html.escape(s.path.name)}</code> — {html.escape(s.reason)}</li>"
        for s in report.skipped
    )
    return f"<h2>Skipped artifacts</h2><ul>{items}</ul>"
