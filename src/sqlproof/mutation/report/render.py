from __future__ import annotations

import html
import json
from dataclasses import asdict

from sqlproof.mutation.report.aggregate import ReportData

_CSS = """
  :root {
    --bg:#0d1f12; --panel:#14291a; --sunken:#0a1a0e;
    --fg:#f0fdf4; --muted:#86efac; --faint:#3d6b4a;
    --accent:#22c55e; --accent2:#4ade80;
    --bad:#ff5f57; --warn:#fcd34d;
    --new-bg:#f59e0b18; --new-bd:#f59e0b66; --new-fg:#fcd34d;
    --border:#22c55e1a; --border2:#22c55e33;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  * { box-sizing:border-box; }
  body {
    margin:0 auto; padding:36px 48px 64px; max-width:1040px;
    background:var(--bg); color:var(--fg);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  }
  .hdr {
    display:flex; align-items:baseline; gap:14px;
    padding-bottom:18px; border-bottom:1px solid var(--border); margin-bottom:4px;
  }
  .logo { font-family:var(--mono); font-size:20px; font-weight:700; color:var(--accent); }
  .label {
    font-family:var(--mono); font-size:12px; text-transform:uppercase;
    letter-spacing:2px; color:var(--accent); opacity:.75;
  }
  h2 {
    font-family:var(--mono); font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:1.5px;
    color:var(--accent); margin:36px 0 12px;
  }
  .muted { color:var(--muted); } .faint { color:var(--faint); }
  .panel {
    background:var(--panel); border:1px solid var(--border);
    border-radius:12px; padding:24px;
  }
  .score-big { font-size:46px; font-weight:800; letter-spacing:-1px; line-height:1; }
  table {
    width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--border); border-radius:10px; overflow:hidden;
  }
  th,td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--border); }
  tr:last-child td { border-bottom:none; }
  th {
    background:var(--sunken); color:var(--muted); font-weight:600;
    font-size:11px; text-transform:uppercase; letter-spacing:.5px;
  }
  tr:hover td { background:#22c55e0d; }
  .badge-new {
    background:var(--new-bg); border:1px solid var(--new-bd); color:var(--new-fg);
    border-radius:99px; padding:1px 9px; font-size:11px; font-weight:700;
    font-family:var(--mono);
  }
  code {
    background:var(--sunken); border:1px solid var(--border2); border-radius:5px;
    padding:2px 7px; font-size:12px; color:var(--accent2); font-family:var(--mono);
  }
  .drift { color:var(--warn); }
  .err { color:var(--bad); font-weight:700; }
  .empty { text-align:center; color:var(--muted); padding:80px 0; }
  a { color:var(--accent2); }
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
        '<!doctype html>\n<html lang="en"><head><meta charset=utf-8>'
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SqlProof Mutation Report</title>"
        f"<style>{_CSS}</style></head><body>"
        "<header class=hdr><span class=logo>SqlProof</span>"
        "<span class=label>// mutation report</span></header>"
        f"{body}"
        f"<script id=report-data type=application/json>{blob}</script>"
        "</body></html>"
    )


def _chart_section(report: ReportData) -> str:
    scored = [r for r in report.runs if r.score is not None]
    if not scored:
        return "<h2>Mutation score over time</h2><p class=muted>No scored runs yet.</p>"
    points: list[tuple[str, float]] = [
        (r.started_at, s) for r in scored if (s := r.score) is not None
    ]
    width, height, pad = 720, 220, 30
    n = len(points)

    def x(i: int) -> float:
        return width / 2 if n == 1 else pad + i * (width - 2 * pad) / (n - 1)

    def y(score: float) -> float:
        return height - pad - score * (height - 2 * pad)

    coords = " ".join(f"{x(i):.1f},{y(s):.1f}" for i, (_, s) in enumerate(points))
    base_y = y(0.0)
    area_pts = f"{x(0):.1f},{base_y:.1f} {coords} {x(n - 1):.1f},{base_y:.1f}"
    grid = "".join(
        f'<line x1="{pad}" y1="{y(v):.1f}" x2="{width - pad}" y2="{y(v):.1f}"'
        ' stroke="var(--border)" stroke-width="1"/>'
        f'<text x="2" y="{y(v) + 3:.1f}" fill="var(--faint)" font-size="10">'
        f"{int(v * 100)}%</text>"
        for v in (0.0, 0.5, 1.0)
    )
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(s):.1f}" r="3.5" fill="var(--accent)"/>'
        for i, (_, s) in enumerate(points)
    )
    drift = "".join(
        f'<line x1="{x(i):.1f}" y1="{pad}" x2="{x(i):.1f}" y2="{height - pad}"'
        ' stroke="var(--warn)" stroke-dasharray="3 3"/>'
        for i, r in enumerate(scored)
        if r.schema_changed
    )
    latest = f"{points[-1][1] * 100:.0f}%"
    return (
        "<h2>Mutation score over time</h2>"
        "<div class=panel>"
        f"<div class=score-big>{latest}"
        "<span class=muted style='font-size:14px;font-weight:400'> latest</span></div>"
        f"<svg viewBox='0 0 {width} {height}' width='100%' style='margin-top:10px'>"
        f"{grid}"
        f"<polygon points='{area_pts}' fill='#22c55e' fill-opacity='0.10'/>"
        f"<polyline fill=none stroke='var(--accent)' stroke-width='2.5' points='{coords}'/>"
        f"{dots}{drift}</svg>"
        "<p class=muted style='margin-bottom:0'>Dashed amber lines mark runs where the"
        " schema fingerprint changed.</p>"
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
        err_cell = f"<td class=err>{r.errored}</td>" if r.errored > 0 else "<td class=muted>0</td>"
        rows.append(
            f"<tr><td>{html.escape(r.started_at)}{drift}</td><td>{sha}</td>"
            f"<td>{score}</td><td>{r.duration_s:.1f}s</td>{err_cell}</tr>"
        )
    return (
        "<h2>Run log</h2><table>"
        "<tr><th>Started</th><th>Commit</th><th>Score</th><th>Duration</th><th>Errors</th></tr>"
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
