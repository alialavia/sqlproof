# Contributing to sqlproof

Thanks for your interest. This document covers everything you need to make a
change, propose it, and see it shipped.

## Development setup

sqlproof targets Python 3.11+ and uses [uv](https://github.com/astral-sh/uv)
for dependency management.

```bash
git clone https://github.com/alialavia/sqlproof.git
cd sqlproof
uv sync --extra dev
```

That installs the runtime deps (`hypothesis`, `psycopg`, `pglast`, `rich`)
and the dev toolchain (`pytest`, `ruff`, `mypy`, `pyright`, `syrupy`,
`mutmut`).

## Running tests

```bash
# Unit tests only — no database required, fast (~3s)
uv run pytest tests/unit

# Full suite with coverage gate (matches CI)
uv run pytest --cov=sqlproof --cov-fail-under=95
```

Integration tests under `tests/integration/` need a Postgres reachable via
`SQLPROOF_TEST_DATABASE_URL`. The simplest path is to use the same
`supabase/postgres` image CI uses:

```bash
docker run -d --name sqlproof-pg \
  -e POSTGRES_PASSWORD=postgres \
  -p 54399:5432 \
  supabase/postgres:15.8.1.040

# Wait for Postgres, then install plpgsql_check (needed by some tests)
until docker exec sqlproof-pg pg_isready -U postgres -d postgres >/dev/null 2>&1; do sleep 2; done
docker exec sqlproof-pg psql -U postgres -d postgres \
  -c "CREATE EXTENSION IF NOT EXISTS plpgsql_check;"

# Run the suite against it
SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54399/postgres \
  uv run pytest

# Tear down when done
docker rm -f sqlproof-pg
```

For Supabase-shaped tests that exercise `auth.uid()` end-to-end, also apply
the GoTrue auth migration — see `.github/workflows/ci.yml` for the exact SQL
the CI step runs.

## Type checking and linting

```bash
uv run ruff check src/ tests/
uv run pyright
uv run mypy src/sqlproof/
```

CI runs all three; fix any reported issues before opening a PR.

## Commit and PR title convention

sqlproof uses [Conventional Commits](https://www.conventionalcommits.org/)
for PR titles. The PR title is what lands on `main` as the squash commit
message, and it's what [release-please](https://github.com/googleapis/release-please)
reads to compute the next version and CHANGELOG entry.

### Format

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

The subject **must start with a lowercase letter** and avoid a trailing
period.

### Accepted types

| Type | Triggers release? | CHANGELOG section |
|---|---|---|
| `feat` | minor bump | Added |
| `fix` | patch bump | Fixed |
| `perf` | patch bump | Performance |
| `docs` | no | hidden |
| `refactor` | no | Changed |
| `test` | no | hidden |
| `chore` | no | hidden |
| `ci` | no | hidden |
| `build` | no | hidden |
| `style` | no | hidden |
| `revert` | per-case | Reverts |
| `!` suffix or `BREAKING CHANGE:` footer | minor bump (in 0.x) | Breaking Changes |

### Scopes

Optional, no enforced allowlist. Common scopes in this repo: `core`,
`plugin`, `contrib`, `runner`, `generator`, `client`, `schema`, `example`,
`docs`, `deps`.

### Enforcement

A PR title linter runs on every PR. If the title doesn't parse as
Conventional Commits, CI fails with a comment explaining what to fix.

Individual commits on your feature branch can be in any style — only the
PR title matters, since we **squash-merge** every PR onto `main`.

## How releases work

sqlproof uses [release-please](https://github.com/googleapis/release-please)
to automate the mechanical parts of releasing. You don't need to manually
edit `CHANGELOG.md`, bump version strings, or create tags.

### The flow

1. You merge a PR via "Squash and merge" with a Conventional Commits title.
2. The release-please workflow runs on every push to `main`. If your PR's
   type triggers a release (`feat` → minor, `fix` → patch, `BREAKING CHANGE`
   → minor in 0.x), release-please opens or updates a **release PR**.
3. The release PR shows the next version and the proposed CHANGELOG entries.
   Bullets are pulled from PR titles since the last release.
4. Read the release PR. Optionally hand-edit bullets into richer prose
   (release-please preserves your edits across re-runs). When ready, merge it.
5. Merging the release PR causes release-please to tag the commit (`vX.Y.Z`)
   and create a GitHub Release.
6. The existing `release.yml` workflow fires on the new tag, runs the test
   suite once more, builds the wheel, and publishes to PyPI via Trusted
   Publisher (OIDC, no secrets required).

Net result: every release goes through a reviewable PR. No manual steps
between merging a feature PR and the next release going live, except for
clicking "Squash and merge" twice (once on the feature, once on the
release).

### Composite actions are user-facing surface

Anything under `.github/actions/**` (e.g.
`setup-supabase-test-db`) is a user-facing artifact — external repos
reference it via `uses: alialavia/sqlproof/.github/actions/<name>@<ref>`.
That ref is usually a tag (`@v0.2.3`), not `@main`, so pinned consumers
**only see changes that ship as a tagged release**.

So changes to `.github/actions/**` must use a release-triggering commit
type:

- ✅ `feat(action): <subject>` — minor bump in 0.x
- ✅ `fix(action): <subject>` — patch bump
- ✅ `perf(action): <subject>` — patch bump
- ❌ `ci: <subject>` — hidden in CHANGELOG, no release triggered
- ❌ `chore: <subject>`, `docs: <subject>`, etc. — same; doesn't ship

The `(action)` scope is conventional but not strictly required — what
matters is the TYPE. CI enforces this rule via the `pr-action-rules`
workflow, which fails the build with an actionable error if a PR
touching `.github/actions/**` uses a hidden type.

**Why this rule exists:** previously, a `ci: bring storage.buckets up
to migrations 0008+` change landed on main and silently shipped to
nobody — pinned consumers stayed on the old action. The rule
prevents a recurrence.

### How release-please pushes tags that trigger downstream workflows

Tags pushed by the default `GITHUB_TOKEN` **do not trigger other
workflows** — GitHub explicitly blocks this to prevent recursive runs. So
release-please uses a token minted from a GitHub App (`sqlproof-releases`)
installed on this repo. Tags pushed with that token DO trigger
downstream workflows, which is what lets `release.yml` fire when
release-please tags `vX.Y.Z`.

To reproduce this setup in a fork:

1. **Create a GitHub App** in your account settings → Developer settings →
   GitHub Apps → New. Permissions: `contents: write`,
   `pull_requests: write`, `metadata: read`. No callback URL needed.
2. **Generate a private key** for the App (one click in the App's settings).
3. **Install the App** on your fork.
4. **Add two repo secrets**: `RELEASE_PLEASE_APP_ID` (the numeric ID) and
   `RELEASE_PLEASE_PRIVATE_KEY` (paste the `.pem` contents).

The `release-please.yml` workflow then mints a short-lived token from the
App on each run via `actions/create-github-app-token@v1`.

### Manually triggering a release publish

If `release.yml` ever fails to fire on a tag (e.g. the GitHub App secrets
expire or a tag was pushed manually), trigger it from the Actions tab:
**Release → Run workflow → Tag to build and publish: `vX.Y.Z`**. The
workflow's `workflow_dispatch` input checks out the tag you specify and
runs the same build + publish pipeline.

### Stability and deprecation policy

This is the contract sqlproof commits to. Read it before you depend on
the library in production-shaped contexts.

#### What's covered by the stability policy

| Surface | Stable? | Notes |
|---|---|---|
| Public Python API: `sqlproof.*` re-exports (`SqlProof`, `sqlproof`, `ExternalTableSpec`, `SqlProofClient`, etc.) | ✅ — see [Pre-1.0 specifics](#pre-10-specifics) below for the 0.x caveat | The surface the README and quickstart docs describe |
| Pytest plugin: fixture names (`proof`, `db`, `supabase_proof`, `supabase_db`) and DSN-resolution order | ✅ | `--sqlproof-database-url` → `$SQLPROOF_DATABASE_URL` → `$SUPABASE_DB_URL` |
| Pytest plugin CLI flags (`--sqlproof-seed`, `--sqlproof-runs`, etc.) | ⚠ Stabilizing — see [#5](https://github.com/alialavia/sqlproof/issues/5) | Currently in flux; will be locked once #5 closes |
| Composite GitHub Actions under `.github/actions/**` (inputs, outputs, behavior) | ✅ | External repos `uses:` these — see [Composite actions are user-facing surface](#composite-actions-are-user-facing-surface) above |
| Failure-counterexample JSON shape in `.sqlproof/failures/*.json` | ✅ | The JSON file is what external tooling reads on a failed property |
| CLI binary (`sqlproof` entry point in `[project.scripts]`) | ⚠ Stabilizing — see [#5](https://github.com/alialavia/sqlproof/issues/5) | Surface acquires real subcommands or gets removed |
| Module-private names (`_*`, anything not exported from `sqlproof/__init__.py`) | ❌ Not stable | Internal — may change in any release |
| Internal SQL parser AST shape (`schema.model.*`) | ❌ Not stable | Internal representation; may change to add features (e.g., [#3](https://github.com/alialavia/sqlproof/issues/3)) |

If you import something not in the table above, you're depending on
internals. That can break in any release without notice.

#### What counts as a breaking change

A change is breaking if a reasonable consumer of one of the ✅-stable
surfaces above would have to update their code (or workflow, or pin) to
keep working. Concretely:

- **Breaking** — renaming or removing a public function/class/fixture;
  changing the signature (parameter names, types, required vs optional)
  of a public function; changing the behavior in a way that violates a
  documented invariant; changing the resolution order of fixtures or
  env vars; changing inputs/outputs of a composite action; changing the
  JSON-counterexample shape in a way that breaks readers.
- **Not breaking** — adding new optional parameters (with sensible
  defaults); adding new public functions/classes/fixtures; adding new
  env vars in the resolution chain so existing vars still work;
  fixing bugs (behavior changes that make the library do what the
  contract already said it did).

#### Pre-1.0 specifics

While sqlproof is in `0.x`:

- **Breaking changes bump minor** (`0.5.x` → `0.6.0`), not major. The
  `release-please-config.json` sets `bump-minor-pre-major: true` to
  enforce this.
- **Breaking changes are still announced.** A breaking change MUST
  carry one of:
  - `!` after the type/scope in the PR title:
    `feat(client)!: rename db.execute to db.exec`
  - A `BREAKING CHANGE:` footer in the PR description (and squash
    commit body)
- **Breaking changes are documented in the CHANGELOG** under a
  `Breaking Changes` section that release-please generates from those
  footers.
- **Deprecations get one minor of warning where feasible.** If renaming
  `proof.foo()` → `proof.bar()`, the previous minor ships both names
  with `foo()` emitting `DeprecationWarning`, the next minor removes
  `foo()` and marks it as a breaking change. This is "where feasible" —
  some renames don't admit a shim and ship as straight breaking
  changes; this is a strong norm, not a hard rule.

The strong-norm bar reflects the project's pre-1.0 state. After 1.0,
deprecation periods become contractual (see [Path to 1.0](#path-to-10)
below).

#### Support window

- **Only the latest `0.x` minor is supported** for security and
  correctness fixes (see [SECURITY.md](./SECURITY.md)).
- Earlier `0.x` versions are not backported to. Upgrading to the
  latest minor is the supported remediation path.
- After 1.0, this section will be updated to declare a backport window.

### Path to 1.0

When the maintainer judges the API stable enough, the transition is:

1. Merge a commit (any type, usually `docs:`) whose PR description ends with
   the footer `Release-As: 1.0.0`. Include the PR description in the squash
   commit body so the footer reaches git history.
2. release-please's next run proposes a `1.0.0` release PR.
3. In the same PR (or a follow-up), flip `bump-minor-pre-major` to `false`
   in `release-please-config.json` so subsequent breaking changes bump major
   (1.x → 2.0) as normal semver.

The 1.0 release becomes an intentional, reviewable moment with a clear
changelog entry.

## Recommended branch protection settings

If you're forking sqlproof and want the same guarantees, configure the
following on `main` in GitHub repo settings:

- **Require a pull request before merging** (no direct pushes to `main`).
- **Require status checks to pass before merging:**
  - `python (3.11)`, `python (3.12)`, `python (3.13)`
  - `docs`
  - `pr-title` (the PR title linter)
- **Require branches to be up to date before merging.**
- **Require linear history.**
- **Allowed merge methods: squash only** (Settings → General → Pull
  Requests; disables the "Merge" and "Rebase" buttons).
- **Auto-merge: enabled repo-wide** so the maintainer can pre-approve the
  release PR and let it auto-merge once CI finishes.

## Reporting bugs, requesting features, asking questions

Open an issue using one of the templates at
<https://github.com/alialavia/sqlproof/issues/new/choose>. The forms ask for
the context maintainers typically need (Python version, Postgres version,
sqlproof version, minimal repro). Filling them out gets you a faster
response.

## Reporting security vulnerabilities

Do **not** open a public issue for security reports. See
[SECURITY.md](./SECURITY.md) for the private disclosure channel.
