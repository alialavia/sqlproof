# Mutation run persistence + local report/dashboard

**Status:** design approved; ready for implementation planning.

## Goal

A local-first version of the mutation-testing "cloud offering" described
in `2026-06-10-mutation-testing-design.md`: persist each mutation run as a
serializable artifact and visualize results — mutation score over time,
per-target breakdown, and survivor drill-down with reproduction commands.

Parallel execution already exists (`run_mutation_tests(..., max_workers=N)`,
a thread pool with clone-per-mutant in `src/sqlproof/mutation/runner.py`).
What is missing, and what this spec adds, is **persistence** (runs are not
saved anywhere today) and **visualization** (no report exists; today a run
yields a Python object and an `assert_no_survivors()` exception message).

This ships inside the `sqlproof` package as an open-source feature. It is
deliberately the foundation the future paid cloud tier builds on: the run
artifact is the same serializable format a remote ingester would consume,
and the aggregation layer is independent of how results were produced.

## Confirmed decisions

1. **Lives inside sqlproof** — a shipped feature, not a side script.
2. **History-aware** — latest run in detail *and* score over time across
   runs ("Codecov for SQL test strength", scaled to local).
3. **Local-only execution for now** — runs execute on the user's machine
   against the `sqlproof-pg` container; artifacts accumulate in a local
   directory. CI can ingest the same artifacts later without redesign.
4. **Static HTML report** — one self-contained file, no server, no new
   runtime dependencies, no CDN fetches.
5. **JSON run artifacts** (over a SQLite store or auto-HTML-per-run) —
   matches the cloud design's "mutants are serializable artifacts"
   constraint.
6. **Trend-first dashboard layout** (layout A) — see "Dashboard layout".

## Architecture

Three units, each independently testable:

1. **Persistence** (`sqlproof/mutation/` — extends existing model + a
   `save_run` function). Turns a completed run into a JSON artifact on disk.
2. **Aggregation** (`sqlproof/mutation/report/`, pure functions). Reads a
   directory of artifacts, produces a view model: per-run scores, per-target
   scores and history, survivor classification, schema-drift annotations.
   No database access, no HTML.
3. **Rendering + CLI** (`sqlproof/mutation/report/` + `sqlproof/cli.py`).
   Turns the view model into a single self-contained HTML file; the
   `sqlproof mutation report` subcommand wires it together.

Data flow: `run_mutation_tests` → `save_run` → artifact JSON files →
`mutation report` reads all artifacts → aggregation view model →
HTML renderer → `mutation-report.html`.

The aggregation layer takes the artifact directory as its only input and
emits a plain data structure. This is the seam the cloud service reuses:
swap "read local JSON files" for "query ingested runs" and the scoring,
trend, and survivor-classification logic is unchanged.

## Run artifact format

A `save_run` seam in `sqlproof.mutation` (invoked by `run_mutation_tests`
when passed `artifact_dir=...`, also callable directly) writes one file per
run: `.sqlproof/mutation-runs/2026-06-11T14-32-05Z-a1b2c3.json`

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

- **`mutant_id` is the cross-run identity** — the existing
  `prepare_mutants` already derives it as
  `sha256(target_name + canonical-deparsed-mutated-AST)[:16]`
  (`src/sqlproof/mutation/apply.py`), using pglast parse+deparse for SQL
  and PL/pgSQL (whitespace-normalized text fallback for unknown languages).
  Because it is built from the parsed AST, not raw text, it already
  survives formatting changes — so score-over-time and new-vs-known
  survivor comparison can key on it directly. No separate identity field
  is needed. (Resolves the mutant-identity open question in the parent
  spec.)
- **`schema_version: 1`** lets the report command (and the future cloud
  ingester) evolve the format without breaking old files.
- **Per-mutant `duration_s`** is added to `MutantOutcome` (the only new
  field on it — `mutant_id`, `target`, `description`, `status`,
  `pytest_exit_code`, `hypothesis_seed`, and `detail` are already present) —
  cheap now,
  needed later for the parallelism story ("which mutants dominate
  wall-clock").
- **Git SHA + dirty flag** captured best-effort (absent outside a git
  repo), so trend points can correlate with commits.
- The directory is **append-only**; nothing rewrites old artifacts.
- `.sqlproof/` gets a `.gitignore` note. Committing run history is
  optional — the report works either way.

## CLI: `sqlproof mutation report`

A new `mutation` subcommand group on the existing CLI
(`src/sqlproof/cli.py`), starting with one subcommand:

```
sqlproof mutation report
    [--runs-dir .sqlproof/mutation-runs]   # where artifacts live
    [--output mutation-report.html]        # self-contained HTML
    [--open]                               # open in browser after writing
```

Behavior:

- Reads every `*.json` artifact in the runs dir.
- Computes mutation score per run: `(killed + unexpected_kill)` over all
  non-error, non-`expected_survivor` mutants. Errors are excluded from the
  denominator but surfaced loudly — an errored run proves nothing.
- Computes per-target scores and survivor sets keyed by `mutant_id`, so
  survivors are marked **new** (first appearance) vs **known** (seen in
  earlier runs).
- Renders a single self-contained HTML file: inline CSS/JS, run data
  embedded as a JSON blob, charts as hand-rolled inline SVG — **zero new
  runtime dependencies**, nothing fetched from a CDN, works offline.
- Implementation in a new `sqlproof/mutation/report/` module, with data
  aggregation separate from HTML rendering (aggregation reusable by the
  future cloud service, testable without parsing HTML).
- Each survivor row shows a copy-pasteable repro command rebuilt from the
  artifact (`pytest <args> --hypothesis-seed=<seed>` plus the mutant id).

## Dashboard layout (trend-first)

One self-contained HTML page, top to bottom:

1. **Mutation score over time** — the hero chart (inline SVG). Overall
   line plus a per-target toggle. This is the first thing you see; the
   primary question the report answers is "is test strength improving?"
2. **Per-target table** — target · score · mutant count · survivor count ·
   inline sparkline.
3. **Survivors (latest run)** — NEW badge for first-seen survivors,
   description, and the repro command.
4. **Run log** — every run: date · git sha · score · duration.

## Error handling & edge cases

- **No runs yet** — reporting against an empty or missing dir writes a
  valid HTML page that says "no runs found" rather than erroring, so the
  command is safe to script before any run exists.
- **Corrupt / unknown-`schema_version` artifacts** — skipped individually
  with a stderr warning that names the bad file; the report still renders
  from the good ones.
- **Schema drift across runs** — if `schema_fingerprint` changes between
  runs, the trend chart still plots but annotates the point where the
  schema changed, so a score jump caused by a different mutant population
  is not misread as a test-strength change.
- **Disappearing targets** — a target present in old runs but absent now
  stops contributing new points (no crash); its history stays visible.
- **Disappearing survivors** — a survivor whose `mutant_id` is gone from
  the latest run drops out of the "current survivors" list but remains in
  that run's archived detail.
- **Errored mutants** — excluded from the score denominator but surfaced
  as a distinct count/badge, never silently folded into "killed".

## Testing strategy

- **Aggregation layer** carries the bulk of coverage: pure functions over
  fixture JSON, no DB, no HTML parsing — score math, new-vs-known survivor
  classification, schema-drift annotation, empty/corrupt-input handling.
- **Artifact round-trip** — `save_run` writes, the aggregator reads it
  back, fields survive (including the new `duration_s`).
- **`mutant_id` stability** — a regression test documenting that the same
  logical mutant with reformatted SQL produces the same `mutant_id` and
  that genuinely different mutants do not collide. This is existing
  `prepare_mutants` behavior, but the report now depends on it for
  cross-run keying, so it is worth pinning.
- **HTML rendering** — one smoke test that the generated file is
  self-contained (no external `http(s)://` references) and embeds the data
  blob; not asserting on visual layout.
- **CLI** — `mutation report` exit codes and the empty-dir path, following
  existing `cli.py` test patterns.

## Out of scope

- Live/streaming dashboard while a run is in progress (the runner would
  need progress events) — static report only.
- CI integration (nightly job, artifact upload, PR comments) — the
  artifact format is designed to support it later without redesign.
- A web server / always-on dashboard — static HTML file only.
- SQLite or any other storage engine — JSON artifacts only.
- Server-side LLM mutant generation, equivalence triage, fan-out across
  ephemeral databases — all cloud-tier concerns from the parent spec.

## Relationship to the cloud offering

This is the local proof of the same signal the cloud tier sells. The
artifact is the wire format a remote ingester consumes; the aggregation
layer is the scoring/trend/triage logic the service reuses verbatim. The
cloud tier adds fan-out compute, hosted history, metered LLM features, and
PR comments on top — none of which this spec implements, all of which it is
shaped to accommodate.
