# Mutation run persistence + local report/dashboard — DRAFT (brainstorm in progress)

**Status:** brainstorming, not yet a final spec. Resume by reading this doc,
then continue the design presentation from "Where we left off" below.

## Goal

A basic personal version of the planned cloud offering (see
`2026-06-10-mutation-testing-design.md`, "Cloud offering" section): run
mutations in parallel (already exists via `max_workers`), persist run
results, and visualize them — mutation score over time, per-target
breakdown, survivor drill-down with repro commands.

## Decisions made (user-confirmed)

1. **Lives inside sqlproof** — shipped open-source feature, the foundation
   the cloud tier builds on later. Not a side script, not a separate repo.
2. **History-aware visualization** — latest run in detail AND score over
   time across runs ("Codecov for SQL test strength", scaled to local).
3. **Local-only for now** — runs execute on the user's machine against the
   sqlproof-pg container; artifacts accumulate in a local directory. CI
   later, without redesign (artifacts are just JSON files).
4. **Static HTML report** — `sqlproof mutation report` writes one
   self-contained HTML file. No server, no new runtime deps, no CDN.
5. **Approach A: JSON run artifacts** (over SQLite store / auto-HTML per
   run) — one JSON file per run in `.sqlproof/mutation-runs/`; matches the
   cloud design's "mutants are serializable artifacts" constraint.

## Section 1: Run artifact format & persistence — APPROVED

A `save_run` seam in `sqlproof.mutation` (used by `run_mutation_tests`
when passed `artifact_dir=...`, also callable directly) writes one file
per run: `.sqlproof/mutation-runs/2026-06-11T14-32-05Z-a1b2c3.json`

```json
{
  "schema_version": 1,
  "run_id": "a1b2c3...",
  "started_at": "2026-06-11T14:32:05Z",
  "duration_s": 412.7,
  "sqlproof_version": "0.9.0",
  "git_sha": "58d0e84",
  "git_dirty": false,
  "hypothesis_seed": 1234567890,
  "schema_fingerprint": "sha256:...",
  "pytest_args": ["-m", "rls", "tests/"],
  "outcomes": [
    {
      "mutant_id": "...",
      "mutant_key": "sha256:...",
      "target": "billing.compute_invoice",
      "description": "COALESCE(SUM(usage), 0) -> 1",
      "status": "killed",
      "pytest_exit_code": 1,
      "hypothesis_seed": 1234567890,
      "duration_s": 8.3,
      "detail": null
    }
  ]
}
```

Key points (all approved):

- **`mutant_key` is the cross-run identity** — hash of (target, normalized
  mutated AST) via pglast fingerprinting (resolves the open question in the
  mutation-testing spec). `mutant_id` stays per-authoring; `mutant_key`
  survives formatting changes, enabling score-over-time and new-vs-known
  survivor classification.
- `schema_version: 1` on the artifact for forward evolution (cloud
  ingester reads the same format later).
- Per-mutant `duration_s` added to `MutantOutcome`.
- Git SHA + dirty flag captured best-effort (absent outside a git repo).
- Runs directory is append-only; old artifacts never rewritten.
- `.sqlproof/` gitignore note; committing history is optional, report
  works either way.

## Section 2: CLI `sqlproof mutation report` — PRESENTED, NOT YET APPROVED

New `mutation` subcommand group on the existing CLI (`src/sqlproof/cli.py`):

```
sqlproof mutation report
    [--runs-dir .sqlproof/mutation-runs]
    [--output mutation-report.html]
    [--open]
```

- Reads all `*.json` artifacts; skips unparseable/unknown-schema_version
  files with a stderr warning (one corrupt artifact must not kill the report).
- Mutation score = (killed + unexpected_kill) / all non-error,
  non-expected-survivor mutants; errors excluded from denominator but
  surfaced loudly.
- Survivors keyed by `mutant_key` → marked **new** (first appearance) vs
  **known** (seen in earlier runs).
- Self-contained HTML: inline CSS/JS, embedded JSON data blob, hand-rolled
  inline SVG charts. Zero new runtime deps, works offline.
- Code in new `sqlproof/mutation/report/` module — aggregation separate
  from HTML rendering (aggregation reusable by future cloud service,
  testable without parsing HTML).
- Each survivor row shows a copy-pasteable repro command rebuilt from the
  artifact (`pytest <args> --hypothesis-seed=<seed>` + mutant id).

## Where we left off / remaining steps

1. **Get approval on Section 2** (user exited right after it was presented).
2. **Section 3: dashboard layout** — present mockups in the browser visual
   companion (user opted in; server was running at .superpowers/brainstorm/,
   restart with the brainstorming skill's start-server.sh). Layout
   candidates to mock: score-over-time chart up top + per-target table +
   survivor detail list; alternatives around how history vs latest-run
   detail share the page.
3. **Sections 4+: error handling & testing strategy**, then full design
   approval.
4. Write final spec (replace this DRAFT file), self-review, commit, user
   review gate.
5. Invoke superpowers:writing-plans for the implementation plan.

## Context notes for resumption

- Parallelism already exists: `run_mutation_tests(..., max_workers=N)`,
  thread pool, clone-per-mutant (`src/sqlproof/mutation/runner.py`).
- `MutationResult.to_dict()` already JSON-serializes outcomes
  (`src/sqlproof/mutation/result.py`).
- Existing CLI has `report`/`replay` subcommands for *counterexamples*
  (`src/sqlproof/reporter/`) — the mutation report is a separate concern;
  follow the same json_io patterns where sensible.
- "Orchestration" question from user was answered: scheduling mutants
  across workers, ephemeral DB lifecycle, retries/timeouts, result
  collection, mutant identity over time, CI integration — vs the
  commodity per-mutant pytest compute.
