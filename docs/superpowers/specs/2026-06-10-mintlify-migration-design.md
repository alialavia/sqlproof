# Mintlify Migration Design

**Date:** 2026-06-10
**Status:** Approved pending user spec review

## Context

The SqlProof website (`website/`) is an Astro Starlight site serving
`sqlproof.com` via GitHub Pages (`deploy-website.yml`, `public/CNAME`). It has
a bespoke Astro landing page (`src/pages/index.astro`), PostHog analytics
injected at build time (`src/posthog.mjs`), custom CSS, and ~30 docs pages of
plain Markdown with `title`/`description` frontmatter (no Starlight-specific
components or aside syntax — conversion is mechanical).

The goal is a more professional documentation site on Mintlify.

## Decisions (made with user)

1. **Scope:** Everything moves to Mintlify. The Astro site is deleted,
   including the custom landing page, which is rebuilt as a Mintlify homepage.
   (Considered and rejected: Astro apex + `docs.sqlproof.com` subdomain (free),
   and Astro apex + `/docs` subpath via Cloudflare Worker (paid Mintlify tier,
   the mintlify.com/momentic.ai pattern). The subpath pattern remains the
   graduation path if a bespoke landing page or full blog is wanted later;
   nothing in this migration blocks it.)
2. **Hosting:** Mintlify's hosted platform, connected to the GitHub repo,
   serving the apex `sqlproof.com`. Auto-deploys on push to main.
3. **WIP handling:** The uncommitted mutation-testing docs work on main is
   committed to its own branch/PR first; the migration builds on top of it.
4. **Content treatment:** Full enhancement — every page is rewritten to use
   Mintlify components where they fit.

## Sequencing

1. Commit the current working-tree changes (mutation-testing docs: `AGENTS.md`,
   `examples/inbox/README.md`, `pyproject.toml`, `uv.lock`,
   `website/astro.config.mjs`, `website/src/content/docs/examples/inbox/index.md`,
   plus 3 new pages and `examples/inbox/tests/mutation/`) to branch
   `docs/mutation-testing-pages`; push and open a PR.
2. Branch `feat/mintlify-docs` from `docs/mutation-testing-pages` so the new
   mutation-testing pages are migrated with everything else and the two PRs
   don't conflict. Merge order: mutation docs PR first, then the migration PR.

## Structure

A new `docs/` directory at the repo root replaces `website/`:

- `docs/docs.json` — Mintlify config: site name `SqlProof`, theme colors
  matching the current green branding (dark `#0d1f12` base palette taken from
  the existing CSS), favicon (port `public/favicon.svg`), GitHub link
  (`https://github.com/alialavia/sqlproof`) in the topbar, navigation (below),
  SEO defaults, and the PostHog integration.
- `docs/index.mdx` — homepage. Carries over the landing page's content: the
  "Find SQL bugs before your users do" hero copy, the `pip install sqlproof`
  snippet, and a `CardGroup` linking to Supabase Quickstart, Getting Started,
  Examples, and API Reference. The Astro/CSS implementation is not ported.
- All 30 content pages convert to `.mdx` at **identical URL paths**:
  `/supabase-quickstart`, `/getting-started`, `/guides/*`, `/api/*`,
  `/examples/*`, `/examples/inbox/*`. Frontmatter is already compatible.
  Note: `/examples/inbox` (currently `examples/inbox/index.md`) becomes
  `docs/examples/inbox.mdx` alongside the `docs/examples/inbox/` folder —
  verify Mintlify resolves this file/folder coexistence during implementation;
  if not, add a redirect in `docs.json`.

The `docs.json` schema and component set are verified against
https://mintlify.com/docs at implementation time, not from memory.

## Navigation

Three tabs, groups mirroring the current sidebar order. Each page lives in
exactly one tab (Mintlify navigation requires this); related pages in other
tabs cross-link in prose.

- **Guides** — Supabase Quickstart, Getting Started; groups: Supabase
  (testing Supabase apps, RLS bug classes), Power-User Guides (FK
  distributions, custom generators, mutation testing, CI/CD, local dev,
  security, vs pgTAP).
- **Examples** — Test Patterns group (five property patterns, testing SQL
  functions, realistic data generation), E-Commerce Orders Walkthrough, and
  the Inbox sample group (overview + 11 numbered pages, including the new
  mutation-scoring page).
- **API Reference** — SqlProof Class, CheckOptions, TableCustomization,
  State Machine, Mutation Testing.

Note: "Stateful Tests" in the current sidebar is `api/state-machine` listed
under Test Patterns; it stays at its `/api/state-machine` URL in the API
Reference tab, and the Examples tab's property-patterns page cross-links it.

## Blog (deferred)

No Blog tab in this migration. When wanted later: a Blog tab with an MDX page
per post and a hand-maintained card index, using Mintlify's changelog format
(dated `<Update>` entries) for RSS (`<page-url>/rss.xml`). If the blog becomes
high-volume, revisit the subpath architecture (marketing site + `/docs` +
`/blog` behind a proxy, the mintlify.com/momentic.ai pattern).

## Content enhancement rules

Every page is rewritten with Mintlify components where they genuinely fit:

- `<Steps>` for sequential setup/install flows
- `<CodeGroup>` for alternative snippets (e.g. pip/uv, psql/SQL editor)
- `<Note>` / `<Warning>` / `<Tip>` for callouts currently expressed as prose
- `<Accordion>` / `<AccordionGroup>` for long reference detail
- `<Card>` / `<CardGroup>` on index/overview pages

**Hard constraint: technical content is preserved verbatim.** No rewording of
claims, version numbers, code, SQL, or CLI output. Enhancement is structural
and presentational only. Each converted page is diff-reviewed against its
source for content drift.

## Analytics

PostHog moves to Mintlify's built-in PostHog integration in `docs.json`. The
public project key is committed (it is designed to be client-side, as the
existing `astro.config.mjs` comment notes); the key value is taken from the
deploy environment / user. `src/posthog.mjs` is deleted with the Astro site.

## Retirement

- Delete `website/` entirely (including `.coverage` and `.mypy_cache`
  artifacts that shouldn't have been there).
- Delete `.github/workflows/deploy-website.yml`. The CNAME file goes with
  `website/public/`.
- Sweep the repo (README, AGENTS.md, CONTRIBUTING, CI workflows, example
  READMEs) for `website/` references and links; update to `docs/` or remove.

## Verification

- `mint dev` local preview during development; visual check of homepage,
  quickstart, one inbox page, one API page.
- `mint broken-links` passes before the PR is opened.
- Optional: a lightweight CI job running `mint broken-links` on PRs touching
  `docs/` (decide during implementation; keep it non-blocking initially).

## Manual steps owned by the user (documented in the PR description)

1. Create the Mintlify project/org in their dashboard; connect the GitHub repo
   pointing at the `docs/` directory.
2. Set `sqlproof.com` as the custom domain; update DNS (away from GitHub
   Pages). The old site stays live until Mintlify serves the domain — PR merge
   and DNS cutover are decoupled, so there is no downtime window by default.
3. Enter/confirm the PostHog key.

## Risks

- **Mintlify schema drift:** `docs.json` schema and components evolve quickly;
  verify against live docs during implementation.
- **URL parity:** preserved paths are the mitigation; any path that must
  change gets a `redirects` entry in `docs.json`. Trailing-slash variants
  (Astro served `/getting-started/`) are expected to be handled by Mintlify's
  routing; spot-check after cutover.
- **Free-tier limits:** confirm the hobby/free tier covers custom apex domain
  + the page count before DNS cutover (it does per current public pricing, but
  verify in the dashboard).
