# Release engineering — design spec

**Date:** 2026-05-30
**Author:** Ali Alavi (brainstormed with Claude Opus 4.7)
**Status:** Draft — awaiting user review before implementation planning
**Implements:** the high-quality-OSS-repo asks (commit convention, auto-semver, automated release process)
**Related issues:** #5 (pytest plugin CLI flags stabilization), #6 (deprecation policy for 0.x series)

## Context

`sqlproof` currently has:

- Hand-maintained `CHANGELOG.md` in [Keep a Changelog](https://keepachangelog.com/) format.
- A `release.yml` workflow that triggers on `v*` tags and publishes to PyPI via Trusted Publisher (OIDC).
- Conventional-commits-style commit messages by convention (`fix(core):`, `feat(contrib):`, etc.) but no enforcement.
- Current PyPI version `0.1.0a1`; CHANGELOG framing is "early-stage alpha, APIs unstable."
- No commit-message linting, no PR title linting, no automated changelog generation, no automated version bumping, no contributor docs, no issue/PR templates, no dependency-update automation, no security or code-of-conduct files.

The maintainer wants to bring the repo to a quality bar comparable to mature open-source Python libraries, with three explicit goals (commit convention, auto-semver based on commits, good release process) and openness to companion improvements that round out the picture.

## Goals

1. **Enforce a commit convention** that release tooling can mechanically interpret.
2. **Bot-assisted releases with human approval**: every release goes through a reviewable "release PR" that proposes the next version and changelog. Maintainer merges it when ready.
3. **Automate the mechanical parts** of releasing (version bump in two places, changelog generation, tag creation, GitHub Release creation, PyPI publish) so they happen the same way every time.
4. **Drop the alpha track** (`0.1.0aN`) in favor of plain `0.x.y` versioning. Communicate "usable, pre-1.0" without the explicit prerelease classifier.
5. **Round out OSS hygiene**: contributor onboarding doc, PR + issue templates, automated dependency updates, security reporting path, code of conduct.

## Non-goals

- **Full automation** (push-to-release without human approval) — explicitly out of scope; the human approval gate on each release is intentional.
- **Migration to 1.0** — out of scope for this work. The path is documented (Section 3 — "Path to 1.0") but staying in 0.x is the working assumption.
- **Pre-release tracks** (alpha/beta/rc) — explicitly out of scope. We're dropping them.
- **Renovate** as a Dependabot alternative — out of scope; Dependabot with a pragmatic uv-lock-refresh workflow is the chosen path.
- **CODEOWNERS** file — skipped for now since the project is solo-maintained.
- **Multi-package releases** — `sqlproof` is a single package; the configuration accommodates only that.

## Architecture — end-to-end release flow

```
contributor opens PR
      ↓
PR title linter (CI check) — fails build if title isn't conventional-commits format
      ↓
PR approved, merged via "Squash and merge"
   └─ PR title becomes the squash commit message on main
      ↓
release-please-action runs on every push to main
   ├─ Reads conventional-commits messages since the last release tag
   ├─ Calculates next version
   │    • feat → minor bump
   │    • fix / perf → patch bump
   │    • `!` or BREAKING CHANGE footer → minor bump (in 0.x; would be major in 1.x+)
   ├─ If no release-worthy commits: does nothing
   ├─ If release-worthy commits AND no open release PR: opens one
   └─ If release-worthy commits AND open release PR exists: updates it
      ↓
The release PR proposes:
   • bump version in pyproject.toml AND src/sqlproof/_version.py
   • prepend new section to CHANGELOG.md
   • on merge: tag the commit as vX.Y.Z and create a GitHub Release
      ↓
maintainer reads the release PR, optionally hand-edits CHANGELOG bullets into prose,
merges when satisfied
      ↓
release-please-action (on the merge) creates the tag + GitHub Release
      ↓
existing release.yml triggers on the new tag
   └─ uv sync → pytest → uv build → pypa/gh-action-pypi-publish via Trusted Publisher
      ↓
PyPI has the new version
```

**Two human decision points:** approving the source PR (as today), and merging the release PR (the new gate). Between them is fully automated.

## Section 1 — Commit convention & PR title linting

### PR title format

```
<type>(<optional-scope>): <subject>

Examples:
  feat(generator): support composite UNIQUE constraints
  fix(core): skip insert when dataset has no rows
  docs(readme): clarify pre-1.0 stability guarantees
  ci: switch to squash-merge
  chore(deps): bump hypothesis to 6.160
  feat(client)!: rename db.execute to db.exec    ← `!` = breaking change
```

### Accepted types and CHANGELOG mapping

| Type | Triggers release? | CHANGELOG section |
|---|---|---|
| `feat` | minor bump | Added |
| `fix` | patch bump | Fixed |
| `perf` | patch bump | Performance |
| `docs` | no | Documentation |
| `refactor` | no | Changed |
| `test` | no | hidden |
| `chore` | no | hidden |
| `ci` | no | hidden |
| `build` | no | hidden |
| `style` | no | hidden |
| `revert` | per-case | Reverts |
| any `!` or `BREAKING CHANGE:` footer | minor bump (in 0.x) | Breaking Changes |

### Scopes

Optional, no enforced allowlist. Scopes observed in current commits: `core`, `plugin`, `contrib`, `runner`, `generator`, `client`, `schema`, `example`, `website`, `deps`, `docs`. Pattern is "subsystem or surface being touched." Enforcing a specific list adds friction with little payoff for a project at this scale.

### Enforcement

- **CI gate:** new workflow `.github/workflows/pr-title.yml` using `amannn/action-semantic-pull-request@v5`. Runs on `pull_request` events. Fails if title doesn't parse as Conventional Commits. Comments on the PR with the specific reason.
- **No commit-message linting** (we're squash-merging, so individual feature-branch commits don't survive on main).
- **Squash-merge only** (enforced via branch protection — see Section 4).

## Section 2 — release-please configuration

### Three new files at repo root + a workflow

**`.github/workflows/release-please.yml`:**

```yaml
name: release-please
on:
  push:
    branches: [main]
permissions:
  contents: write       # tag + GitHub Release
  pull-requests: write  # open/update the release PR
jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@v4
        with:
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json
```

**`release-please-config.json`:**

```json
{
  "packages": {
    ".": {
      "package-name": "sqlproof",
      "release-type": "python",
      "changelog-path": "CHANGELOG.md",
      "bump-minor-pre-major": true,
      "bump-patch-for-minor-pre-major": false,
      "extra-files": [
        { "type": "generic", "path": "src/sqlproof/_version.py" }
      ],
      "changelog-sections": [
        { "type": "feat",     "section": "Added" },
        { "type": "fix",      "section": "Fixed" },
        { "type": "perf",     "section": "Performance" },
        { "type": "refactor", "section": "Changed" },
        { "type": "docs",     "section": "Documentation" },
        { "type": "revert",   "section": "Reverts" },
        { "type": "test",     "hidden": true },
        { "type": "chore",    "hidden": true },
        { "type": "ci",       "hidden": true },
        { "type": "build",    "hidden": true },
        { "type": "style",    "hidden": true }
      ]
    }
  }
}
```

**`.release-please-manifest.json`:**

```json
{ ".": "0.1.0a1" }
```

This declares "the last released version was 0.1.0a1." release-please computes next version from commits since the git tag `v0.1.0a1`.

### Why these settings

- `bump-minor-pre-major: true` → breaking changes stay in 0.x by bumping minor (0.1.x → 0.2.0), not jumping to 1.0. Matches the CHANGELOG's current stance and #6's pending deprecation-policy work.
- `bump-patch-for-minor-pre-major: false` (the default; included for explicitness) → `feat:` commits still bump minor in 0.x (0.1.x → 0.2.0), not patch.
- `extra-files` → release-please bumps the version string in `src/sqlproof/_version.py` in lockstep with `pyproject.toml`. Requires adding a `# x-release-please-version` marker comment in `_version.py` next to the line to be rewritten.
- `changelog-sections` → maps commit types to Keep-a-Changelog-style headings. Hidden types stay out of CHANGELOG entirely.

### Path to 1.0

Three ways, in recommended order:

1. **`Release-As:` footer in a deliberate commit (recommended)** — merge a commit (any type, usually `docs:` or `chore:`) with footer `Release-As: 1.0.0`. release-please uses that as the next version regardless of auto-calculation. After 1.0 merges, flip `bump-minor-pre-major` to `false` so subsequent breaking changes bump major (2.0).
2. **Flip the config and let a `feat!:` do it** — turn off `bump-minor-pre-major`; next breaking-change commit triggers 0.x → 1.0 automatically. Less explicit.
3. **Manual manifest edit** — change `"."`: `"0.x.y"` to `"."`: `"1.0.0"` directly. Bluntest tool.

### Bootstrap

The first release-please run after this setup ships will produce a release PR for **0.2.0** (signals "past alpha, accumulated work since 0.1.0a1"). Achieved by adding a `chore: graduate from alpha` commit with `Release-As: 0.2.0` footer to the rollout PR; release-please picks it up on its first run.

## Section 3 — CHANGELOG.md migration

### Three migration moves, in order

1. **Delete the `## [Unreleased]` section.** The commits it describes will be picked up by release-please from git history and proposed in the first release PR. Leaving the hand-written `[Unreleased]` section would produce duplicated entries (one hand-written, one auto-generated) once release-please runs.

2. **Keep `## [0.1.0a1] - 2026-05-04` exactly as-is.** release-please prepends new entries above its insertion point; it doesn't touch what's below. The hand-written prose for the historical 0.1.0a1 release survives verbatim. Going forward, only entries from 0.2.0 onward will be in release-please's auto-generated style.

3. **Remove the bottom `[Unreleased]` reference link.** release-please uses inline per-commit/per-PR links in its generated entries, so the `[Unreleased]: .../compare/...` link becomes dead weight. The `[0.1.0a1]: .../tag/v0.1.0a1` link stays since it's referenced from the surviving 0.1.0a1 heading.

### What the first release PR will produce

Approximately (date placeholder filled in by release-please at release time, PR numbers verified against git log):

```markdown
# Changelog

## [0.2.0] - YYYY-MM-DD

### Added
* pytest plugin fixtures (#15)
* PL/pgSQL coverage contrib (#14)
* as_rls_user helper (#17)
* @sqlproof decorator with columns= and dataset (#16)

### Fixed
* skip insert when dataset has no rows (#23)
* fall back to default PostHog host on empty string (#21)
* lazy-import sqlproof inside fixtures to avoid coverage drop (#15)

## [0.1.0a1] - 2026-05-04
[... existing hand-written prose stays exactly as-is ...]
```

### Trade-off

Future CHANGELOG entries will lean tersely-bulleted by default. **The release PR is editable** — before merging, you can hand-edit bullets into richer prose and release-please preserves manual edits across re-runs. So the auto-generation is a floor (always something usable), not a ceiling (you can always polish before publishing).

## Section 4 — Branch protection + PyPI publish handoff

### Branch protection on `main`

Configured in GitHub repo settings (no files to commit; documented in `CONTRIBUTING.md`):

- ✅ Require a pull request before merging (no direct pushes)
- ✅ Require status checks to pass before merging:
  - `python (3.11)`, `python (3.12)`, `python (3.13)`
  - `website`
  - `pr-title` (the new PR title linter)
- ✅ Require branches to be up to date before merging
- ✅ Require linear history
- ✅ Allowed merge methods: **squash only** (Settings → General → Pull Requests; disables "Merge" and "Rebase" buttons)
- ❌ No bypass actors — release-please works inside the normal PR flow

**Auto-merge:** enable repo-wide so the maintainer can pre-approve the release PR and let it auto-merge once CI finishes.

### Chain of events when a release PR is merged

```
Maintainer clicks "Squash and merge" on the release PR
   │  (title: "chore(main): release 0.2.0")
   ↓
PR title linter passes (chore is valid)
   ↓
CI passes (full suite ran on the release PR's commits already)
   ↓
Squash commit lands on main
   ↓
release-please-action fires on the push
   ├─ Sees the merged release PR
   ├─ Creates git tag v0.2.0 on the merge commit
   └─ Creates a GitHub Release with the changelog excerpt
   ↓
existing release.yml fires on the new v0.2.0 tag
   ├─ uv sync --extra dev
   ├─ pytest -m "not nocover"  ← second test run; defense in depth
   ├─ uv build
   └─ pypa/gh-action-pypi-publish via OIDC (Trusted Publisher — already configured)
   ↓
sqlproof 0.2.0 on PyPI
```

### Notes on this handoff

- **`release.yml` doesn't change at all.** Its `on: push: tags: ["v*"]` trigger catches the tag release-please creates.
- **`environment: pypi`** on the publish job is a powerful safety lever. To add a manual approval gate before PyPI publish (separate from merging the release PR), configure required reviewers on the `pypi` environment in repo settings — `pypa/gh-action-pypi-publish` will block until approved. Out of scope for this work but worth knowing it's available.

## Section 5 — Supporting files (OSS hygiene)

### `CONTRIBUTING.md` (repo root)

The canonical onboarding doc. Sections:

- **Dev setup**: `uv sync --extra dev`, optional Docker for integration tests.
- **Running tests**: unit / integration / coverage gate / how to spin up the local Postgres for integration tests.
- **Commit + PR title convention**: link to the conventional-commits spec, examples from the table in Section 1.
- **Release process**: what release-please does, when release PRs appear, how to read and merge them.
- **Path to 1.0**: the `Release-As:` footer trick from Section 2.
- **Recommended branch protection settings**: so anyone who forks knows what to enable.

### `.github/PULL_REQUEST_TEMPLATE.md`

Pre-fills the PR body. Three sections:

- **Summary** — what changed, in bullets
- **Test plan** — `- [ ]` checklist of what was run/checked locally
- **Related** — issues closed, PRs depended on, follow-ups filed

### `.github/ISSUE_TEMPLATE/` directory

Four files using GitHub's `.yml` template syntax:

- `bug_report.yml` — repro steps, expected behavior, actual behavior, Python version, Postgres version, sqlproof version
- `feature_request.yml` — problem, proposed solution, alternatives considered
- `question.yml` — for usage questions (lighter form, mostly free text)
- `config.yml` — `blank_issues_enabled: false` so users have to pick a template

### `.github/dependabot.yml`

Three ecosystems, weekly cadence:

- `pip` against `pyproject.toml` — bumps declared deps (hypothesis, psycopg, pglast, rich, dev deps)
- `github-actions` — bumps `actions/checkout@v4` → `v5` etc.
- `docker` against `ci.yml` — bumps the `supabase/postgres:15.8.1.040` pin as new tags release

**Honest note on uv:** Dependabot's native `uv` lockfile support is still emerging. The pragmatic setup is to point Dependabot at `pyproject.toml` (pip ecosystem) and add a tiny companion workflow (`.github/workflows/uv-lock-refresh.yml`) that runs `uv lock` on Dependabot PRs and pushes the updated `uv.lock` back to the PR branch. If uv-native Dependabot support lands later, swap. If first-class uv lockfile bumping is wanted today, Renovate handles it — but Renovate is its own substantial config surface. Pragmatic Dependabot path is the recommended option.

### `SECURITY.md` (repo root)

GitHub shows a "Security" badge in repo insights when present. ~30 lines:

- **Supported versions table**: "Latest 0.x — security fixes ship as patches; pre-1.0 we don't backport."
- **Reporting a vulnerability**: prefer GitHub's private security advisories (`Security` tab → `Report a vulnerability`).
- **Fallback contact**: `al@generativemodels.ai`.
- **Expected response time**: best-effort acknowledgement within 5 business days. (Number is a starting commitment; maintainer can adjust as the project's user base grows or shrinks.)

### `CODE_OF_CONDUCT.md` (repo root)

Verbatim [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) with `al@generativemodels.ai` as the contact. ~130 lines, standard text. GitHub shows a "Community" badge when present.

## Rollout plan (proposed PR decomposition)

This work is too large for one PR. Proposed five-PR sequence, in dependency order:

1. **PR title linter only.** Smallest possible first step. Adds `.github/workflows/pr-title.yml`. Test by opening a PR with a bad title and confirming it fails. Doesn't enforce anything else yet — just produces a signal. Allows the rest of the work to "dogfood" the linter.

2. **release-please bootstrap.** Adds release-please workflow, config, manifest, marker comment in `_version.py`, CHANGELOG migration (delete `[Unreleased]`, keep historical sections). The PR description ends with a `Release-As: 0.2.0` footer; **when squash-merging, the maintainer must include the PR description in the squash commit body** (GitHub's default — visible as the "extended description" field in the squash-merge dialog) so the footer reaches git history where release-please reads it. After this PR merges, release-please opens the 0.2.0 release PR. **Don't merge the 0.2.0 release PR until step 7** — first verify the rest of the pipeline works end-to-end.

3. **Branch protection + CONTRIBUTING.md.** Document the new conventions and enable the branch protection rules. Adds `CONTRIBUTING.md`; settings are clicked through in GitHub UI.

4. **PR + issue templates.** Adds `.github/PULL_REQUEST_TEMPLATE.md` and `.github/ISSUE_TEMPLATE/*.yml`. Standalone; no dependencies.

5. **Dependabot + uv-lock-refresh workflow.** Adds `.github/dependabot.yml` and the companion workflow. Will start opening dep-bump PRs soon after merge; observe them, tune cadence/limits if needed.

6. **`SECURITY.md` + `CODE_OF_CONDUCT.md`.** Lowest-risk OSS hygiene additions; just documentation. Can ship alone or bundled with PR 5.

7. **Merge the 0.2.0 release PR.** First real release through the new pipeline. End-to-end verification that release.yml's existing publish flow correctly catches the tag release-please creates.

PR 1 needs to land first (the linter is in CI for all subsequent PRs). The linter doesn't apply to PR 1 itself — there's no workflow yet to enforce it on the very PR that introduces the workflow — so PR 1's own title is on the honor system. PRs 2–6 are largely independent and can interleave. PR 7 is the terminal step.

## Open questions / future work

- **Auto-merge of dependabot PRs**: out of scope. Could be added later with a workflow like `actions-ecosystem/action-add-labels` + Mergify or similar, but starts adding complexity.
- **Pypi environment approval gate**: the infrastructure exists (`environment: pypi` already on the publish job); enabling required reviewers is a one-click GitHub repo settings change. Recommend doing this manually if the maintainer wants extra safety before any publish.
- **`workflow_dispatch` override on release-please**: release-please supports a manual trigger that lets the maintainer specify a version directly. Useful for emergency releases or to skip auto-calculation. Not in v1; can be added later if the need arises.
- **CODEOWNERS**: skipped for now (solo project). Worth adding if other maintainers join.
- **GitHub Discussions**: not currently enabled on the repo; if enabled later, the issue template `config.yml` can link to it. Out of scope for this work.
