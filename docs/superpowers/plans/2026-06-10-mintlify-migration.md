# Mintlify Docs Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Astro Starlight site in `website/` with a Mintlify docs site in `docs/`, serving sqlproof.com from Mintlify's hosted platform with all 32 pages enhanced and at identical URLs.

**Architecture:** Two stacked branches: the uncommitted mutation-testing docs work lands first (`docs/mutation-testing-pages` PR), then `feat/mintlify-docs` builds on it — converting every page to MDX at the same path, adding `docs.json`, and deleting the Astro site. Mintlify hosting/DNS is a manual dashboard step decoupled from the merge.

**Tech Stack:** Mintlify (`docs.json`, MDX), `mint` CLI (requires Node ≥20.17), GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-06-10-mintlify-migration-design.md`

**Verified facts (2026-06-10):** CLI is `npm i -g mint`; preview is `mint dev`; link check is `mint broken-links` (add `--check-anchors` for anchor validation). `docs.json` requires `theme`, `name`, `colors.primary`, `navigation`. PostHog block is `integrations.posthog.apiKey`. Navbar links are `navbar.links[{label,icon,href}]`. Redirects are `redirects[{source,destination}]`.

---

## Content enhancement recipe (used by Tasks 5–9)

Apply to every page. **Hard constraint: technical content verbatim — never reword claims, version numbers, code, SQL, or CLI output.** Only structure changes.

1. `git mv` the file from `website/src/content/docs/<path>.md` to `docs/<path>.mdx` (same path = same URL).
2. Keep frontmatter `title` and `description` exactly as-is.
3. Apply components **only where the pattern matches**:
   - Numbered sequential sections (`## 1. Install`, `## 2. Point SqlProof at…`) → `<Steps><Step title="Install">…</Step></Steps>`; strip the number from the title.
   - Adjacent alternative code blocks for the same action (e.g. pip vs uv, psql vs SQL editor) → `<CodeGroup>` with a title per block. Do NOT group unrelated snippets.
   - Prose starting with "Note:", "Warning:", "Important:", "Tip:" or clearly cautionary sentences → `<Note>`, `<Warning>`, `<Tip>`. One component per callout; don't nest.
   - Long enumerations of reference detail (option tables with per-option prose, FAQ-ish sections) → `<AccordionGroup><Accordion title="…">` only when a section exceeds ~15 lines and is skippable reference material.
   - Overview/index pages listing child pages → `<CardGroup cols={2}>` with one `<Card title icon href>` per child.
4. Escape MDX hazards: `<` followed by a letter in prose (e.g. `<your-project>`) must become `` `<your-project>` `` inline code or `&lt;`; `{` in prose must be escaped or fenced. Code fences are already safe.
5. After each file: it renders in `mint dev` with no MDX error in the terminal, and content diff vs. the source shows only structural changes (`git diff --word-diff HEAD~1 -- <file>` after commit, or compare before committing).

Worked example (from `supabase-quickstart.md`):

````mdx
## 1. Install

Make sure your project has Python 3.11+ and `pytest`. Then:

```bash
pip install sqlproof
```
````

becomes

````mdx
<Steps>
  <Step title="Install">
    Make sure your project has Python 3.11+ and `pytest`. Then:

    ```bash
    pip install sqlproof
    ```
  </Step>
  ...remaining numbered sections as Steps...
</Steps>
````

---

### Task 1: Land the WIP mutation-testing docs on its own branch

**Files:** all currently-dirty/untracked files (no edits, just commits): `AGENTS.md`, `examples/inbox/README.md`, `pyproject.toml`, `uv.lock`, `website/astro.config.mjs`, `website/src/content/docs/examples/inbox/index.md`, `examples/inbox/tests/mutation/`, `website/src/content/docs/api/mutation-testing.md`, `website/src/content/docs/examples/inbox/mutation-scoring.md`, `website/src/content/docs/guides/mutation-testing.md`

- [ ] **Step 1: Branch from main with the working tree intact**

```bash
git checkout main
git checkout -b docs/mutation-testing-pages
```

(Dirty files travel with the checkout; the spec branch `docs/mintlify-migration-design` keeps its own commit.)

- [ ] **Step 2: Commit everything**

```bash
git add -A
git status --short   # expect: only the 10 paths above, all staged
git commit -m "docs(mutation): mutation testing guides, API reference, and inbox scoring example"
```

- [ ] **Step 3: Sanity-check the Astro build still passes**

```bash
cd website && npm ci && npm run build && cd ..
```

Expected: `astro build` completes without errors (the WIP edited `astro.config.mjs`).

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin docs/mutation-testing-pages
gh pr create --title "docs(mutation): mutation testing docs pages" \
  --body "Mutation-testing guides, API reference page, and inbox mutation-scoring example. Precedes the Mintlify migration PR, which builds on this branch."
```

### Task 2: Create the migration branch

- [ ] **Step 1: Branch from the mutation-docs branch and bring the spec + plan along**

```bash
git checkout -b feat/mintlify-docs docs/mutation-testing-pages
git cherry-pick docs/mintlify-migration-design   # single commit holding spec + this plan
git log --oneline -3   # expect: spec/plan commit, mutation docs commit, 0.9.0
```

### Task 3: Scaffold docs.json and verify it serves

**Files:**
- Create: `docs/docs.json`
- Create: `docs/favicon.svg` (copy of `website/public/favicon.svg`)

- [ ] **Step 1: Install the CLI**

```bash
node --version   # need >= 20.17
npm i -g mint
```

- [ ] **Step 2: Copy the favicon**

```bash
cp website/public/favicon.svg docs/favicon.svg
```

- [ ] **Step 3: Write `docs/docs.json`** (complete file; nav pages must exist before `mint dev` is clean, so groups fill in as Tasks 4–9 land — start with only `index` and add pages per task, OR create all pages first as moves then enhance. This plan moves files per-batch; add each batch's pages to `docs.json` in that batch's task. Initial file:)

```json
{
  "$schema": "https://mintlify.com/docs.json",
  "theme": "mint",
  "name": "SqlProof",
  "colors": {
    "primary": "#16a34a",
    "light": "#22c55e",
    "dark": "#15803d"
  },
  "favicon": "/favicon.svg",
  "navbar": {
    "links": [
      { "label": "GitHub", "icon": "github", "href": "https://github.com/alialavia/sqlproof" }
    ]
  },
  "navigation": {
    "tabs": [
      {
        "tab": "Guides",
        "groups": [
          { "group": "Get Started", "pages": ["index"] }
        ]
      }
    ]
  }
}
```

- [ ] **Step 4: Stub `docs/index.mdx`** (replaced by Task 4)

```mdx
---
title: "SqlProof"
description: "Property-based testing for PostgreSQL"
---

Placeholder — replaced in the homepage task.
```

- [ ] **Step 5: Verify it serves**

```bash
cd docs && mint dev
```

Expected: local preview at `http://localhost:3000` renders the stub with SqlProof name, green theme, GitHub navbar link. Ctrl-C after checking.

- [ ] **Step 6: Commit**

```bash
git add docs/docs.json docs/favicon.svg docs/index.mdx
git commit -m "feat(docs): scaffold Mintlify docs.json with SqlProof branding"
```

### Task 4: Homepage

**Files:**
- Modify: `docs/index.mdx` (replace stub)

- [ ] **Step 1: Write the homepage.** Source copy is `website/src/pages/index.astro` (hero + sections). Complete file:

````mdx
---
title: "Find SQL bugs before your users do"
description: "Property-based testing for PostgreSQL. Generates random valid datasets that respect your schema — then tries to break your queries."
mode: "custom"
---

# Find SQL bugs before your users do

Property-based testing for PostgreSQL. SqlProof generates random valid
datasets that respect your schema — then tries to break your queries.

```bash
pip install sqlproof
```

Your queries work on your fixtures. Do they work on real data? Hand-crafted
test data misses edge cases. SqlProof generates thousands of valid random
datasets and finds the minimal counterexample that breaks your invariants.

<CardGroup cols={2}>
  <Card title="Test your Supabase project in 60s" icon="bolt" href="/supabase-quickstart">
    Install, point at your DB, ask your AI agent to write the tests.
  </Card>
  <Card title="Getting Started" icon="rocket" href="/getting-started">
    General setup for any PostgreSQL project.
  </Card>
  <Card title="Examples" icon="flask" href="/examples/property-patterns">
    Five property patterns, walkthroughs, and a full Supabase sample app.
  </Card>
  <Card title="API Reference" icon="code" href="/api/sqlproof-class">
    SqlProof class, CheckOptions, customization, mutation testing.
  </Card>
</CardGroup>
````

Note: `mode: "custom"` hides the sidebar for a landing feel — if it renders poorly in `mint dev`, drop the `mode` line and keep the standard layout. Pull any additional hero/section copy worth keeping directly from `index.astro` (verbatim).

- [ ] **Step 2: Verify in `mint dev`, then commit**

```bash
git add docs/index.mdx
git commit -m "feat(docs): Mintlify homepage from landing page copy"
```

### Task 5: Convert entry pages (heaviest enhancement)

**Files:**
- Move+enhance: `website/src/content/docs/supabase-quickstart.md` → `docs/supabase-quickstart.mdx`
- Move+enhance: `website/src/content/docs/getting-started.md` → `docs/getting-started.mdx`
- Modify: `docs/docs.json` (Get Started group)

- [ ] **Step 1: Move both files**

```bash
git mv website/src/content/docs/supabase-quickstart.md docs/supabase-quickstart.mdx
git mv website/src/content/docs/getting-started.md docs/getting-started.mdx
```

- [ ] **Step 2: Apply the enhancement recipe to both** (these have numbered install flows → `<Steps>`; install alternatives → `<CodeGroup>`)

- [ ] **Step 3: Update the Get Started group in `docs/docs.json`**

```json
{ "group": "Get Started", "pages": ["index", "supabase-quickstart", "getting-started"] }
```

- [ ] **Step 4: Verify and commit**

```bash
cd docs && mint broken-links; cd ..
git add -A docs website
git commit -m "feat(docs): migrate quickstart and getting-started to Mintlify"
```

Expected: `mint broken-links` reports no broken links (links to not-yet-migrated pages WILL be broken until Task 9 — note the count, it must only shrink, and reach zero by Task 10).

### Task 6: Convert guides (9 pages)

**Files:**
- Move+enhance each of: `guides/supabase.md`, `guides/supabase-rls-bug-classes.md`, `guides/fk-distributions.md`, `guides/custom-generators.md`, `guides/mutation-testing.md`, `guides/ci-cd.md`, `guides/local-dev.md`, `guides/security.md`, `guides/vs-pgtap.md` from `website/src/content/docs/` → `docs/` (same relative path, `.mdx`)
- Modify: `docs/docs.json`

- [ ] **Step 1: Move all nine**

```bash
mkdir -p docs/guides
for f in supabase supabase-rls-bug-classes fk-distributions custom-generators mutation-testing ci-cd local-dev security vs-pgtap; do
  git mv "website/src/content/docs/guides/$f.md" "docs/guides/$f.mdx"
done
```

- [ ] **Step 2: Apply the enhancement recipe to each file** (one at a time; check each in `mint dev` before the next)

- [ ] **Step 3: Add groups to the Guides tab in `docs/docs.json`**

```json
{ "group": "Supabase", "pages": ["guides/supabase", "guides/supabase-rls-bug-classes"] },
{ "group": "Power-User Guides", "pages": ["guides/fk-distributions", "guides/custom-generators", "guides/mutation-testing", "guides/ci-cd", "guides/local-dev", "guides/security", "guides/vs-pgtap"] }
```

- [ ] **Step 4: Verify and commit**

```bash
cd docs && mint broken-links; cd ..
git add -A docs website && git commit -m "feat(docs): migrate guides to Mintlify"
```

### Task 7: Convert API reference (5 pages)

**Files:** as Task 6 for `api/sqlproof-class.md`, `api/check-options.md`, `api/table-customization.md`, `api/state-machine.md`, `api/mutation-testing.md`

- [ ] **Step 1: Move**

```bash
mkdir -p docs/api
for f in sqlproof-class check-options table-customization state-machine mutation-testing; do
  git mv "website/src/content/docs/api/$f.md" "docs/api/$f.mdx"
done
```

- [ ] **Step 2: Apply the recipe** (reference pages: lean on `<AccordionGroup>` for long option lists; `<Note>` for caveats; no `<Steps>`)

- [ ] **Step 3: Add the API Reference tab to `docs/docs.json`**

```json
{
  "tab": "API Reference",
  "groups": [
    { "group": "API Reference", "pages": ["api/sqlproof-class", "api/check-options", "api/table-customization", "api/state-machine", "api/mutation-testing"] }
  ]
}
```

- [ ] **Step 4: Verify and commit**

```bash
cd docs && mint broken-links; cd ..
git add -A docs website && git commit -m "feat(docs): migrate API reference to Mintlify"
```

### Task 8: Convert examples (4 pages)

**Files:** as Task 6 for `examples/property-patterns.md`, `examples/testing-sql-functions.md`, `examples/data-generation.md`, `examples/orders.md`

- [ ] **Step 1: Move**

```bash
mkdir -p docs/examples
for f in property-patterns testing-sql-functions data-generation orders; do
  git mv "website/src/content/docs/examples/$f.md" "docs/examples/$f.mdx"
done
```

- [ ] **Step 2: Apply the recipe.** In `property-patterns.mdx`, add a prose cross-link to `/api/state-machine` where stateful testing is discussed (the spec moves "Stateful Tests" out of this nav area).

- [ ] **Step 3: Add the Examples tab (before API Reference) in `docs/docs.json`**

```json
{
  "tab": "Examples",
  "groups": [
    { "group": "Test Patterns", "pages": ["examples/property-patterns", "examples/testing-sql-functions", "examples/data-generation"] },
    { "group": "Walkthroughs", "pages": ["examples/orders"] }
  ]
}
```

- [ ] **Step 4: Verify and commit**

```bash
cd docs && mint broken-links; cd ..
git add -A docs website && git commit -m "feat(docs): migrate example pages to Mintlify"
```

### Task 9: Convert the Inbox sample (12 pages)

**Files:**
- Move+enhance: `examples/inbox/index.md` → `docs/examples/inbox.mdx` (**path change**: index file → named file, same URL `/examples/inbox`)
- Move+enhance the 11 numbered pages from `website/src/content/docs/examples/inbox/` → `docs/examples/inbox/` (`.mdx`)
- Modify: `docs/docs.json`

- [ ] **Step 1: Move**

```bash
mkdir -p docs/examples/inbox
git mv website/src/content/docs/examples/inbox/index.md docs/examples/inbox.mdx
for f in tenant-scoped-vector-search correlated-rls-subqueries idempotent-status-triggers outer-joins-and-where internal-message-rls stable-vector-pagination equivalent-query-optimization stateful-ticket-lifecycle mass-assignment-without-with-check missing-delete-policy mutation-scoring; do
  git mv "website/src/content/docs/examples/inbox/$f.md" "docs/examples/inbox/$f.mdx"
done
```

- [ ] **Step 2: Apply the recipe.** `inbox.mdx` is an overview → `<CardGroup>` of the 11 children.

- [ ] **Step 3: Add the inbox group to the Examples tab**

```json
{ "group": "Inbox Sample (Supabase)", "pages": ["examples/inbox", "examples/inbox/tenant-scoped-vector-search", "examples/inbox/correlated-rls-subqueries", "examples/inbox/idempotent-status-triggers", "examples/inbox/outer-joins-and-where", "examples/inbox/internal-message-rls", "examples/inbox/stable-vector-pagination", "examples/inbox/equivalent-query-optimization", "examples/inbox/stateful-ticket-lifecycle", "examples/inbox/mass-assignment-without-with-check", "examples/inbox/missing-delete-policy", "examples/inbox/mutation-scoring"] }
```

- [ ] **Step 4: Verify `/examples/inbox` resolves with the sibling folder present** in `mint dev`. If the file/folder coexistence fails, rename to `docs/examples/inbox/overview.mdx`, use `examples/inbox/overview` in nav, and add to `docs.json`:

```json
"redirects": [{ "source": "/examples/inbox", "destination": "/examples/inbox/overview" }]
```

- [ ] **Step 5: Verify and commit**

```bash
cd docs && mint broken-links; cd ..
git add -A docs website && git commit -m "feat(docs): migrate inbox sample to Mintlify"
```

### Task 10: Retire the Astro site

**Files:**
- Delete: `website/` (everything), `.github/workflows/deploy-website.yml`
- Modify: `.github/workflows/ci.yml:61-73` (the `website` job), `CONTRIBUTING.md:113`, `CONTRIBUTING.md:304`

- [ ] **Step 1: Confirm nothing remains in `website/src/content/docs/`**

```bash
find website/src/content/docs -name '*.md' | wc -l   # expect 0
```

- [ ] **Step 2: Delete**

```bash
git rm -r website .github/workflows/deploy-website.yml
rm -rf website   # clears untracked leftovers (.coverage, .mypy_cache, node_modules)
```

- [ ] **Step 3: Replace the `website` CI job in `.github/workflows/ci.yml`** (current job at lines 61–73) with:

```yaml
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: 20
      - run: npm i -g mint
      - run: mint broken-links
        working-directory: docs
```

- [ ] **Step 4: Update CONTRIBUTING.md commit scopes** — replace the `website` scope with `docs` at both occurrences (lines 113 and 304).

- [ ] **Step 5: Sweep for stale references**

```bash
grep -rn 'website/' README.md AGENTS.md CONTRIBUTING.md .github/ examples/ --include='*'  | grep -v node_modules
```

Expected: no hits (fix any found — update to `docs/` or remove).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(docs)!: retire Astro site; docs now served by Mintlify

BREAKING CHANGE: website/ removed; GitHub Pages deploy retired. sqlproof.com
serves from Mintlify after DNS cutover."
```

### Task 11: Final verification and PR

- [ ] **Step 1: Full link check**

```bash
cd docs && mint broken-links --check-anchors
```

Expected: zero broken links.

- [ ] **Step 2: Visual pass in `mint dev`** — homepage, supabase-quickstart (Steps render), one guide, `api/check-options` (accordions), `examples/inbox` (cards), one numbered inbox page. All three tabs navigate.

- [ ] **Step 3: Content-drift audit** — for each converted file:

```bash
git log --follow --oneline docs/supabase-quickstart.mdx   # confirms history followed the move
```

Spot-check three pages with `git diff <move-commit>^:<old-path> <move-commit>:<new-path>` equivalents or side-by-side read: only structural changes.

- [ ] **Step 4: Push and open the PR (stacked on the mutation-docs PR)**

```bash
git push -u origin feat/mintlify-docs
gh pr create --base main --title "feat(docs): migrate documentation site to Mintlify" --body "$(cat <<'EOF'
Replaces the Astro Starlight site (website/) with a Mintlify docs site (docs/).
All 32 pages migrated at identical URLs with component enhancement
(technical content verbatim). Astro site, GitHub Pages workflow, and CNAME removed.

Stacked on #<mutation-docs-PR-number> — merge that first.

## Manual steps after merge (owner)
1. Mintlify dashboard: create project, connect this repo, docs directory `docs/`.
2. Set custom domain `sqlproof.com`; update DNS away from GitHub Pages.
   The old site stays live until DNS cutover — no downtime window.
3. PostHog: add to docs/docs.json (key is public/client-side by design):
   `"integrations": { "posthog": { "apiKey": "<PUBLIC_POSTHOG_KEY>" } }`
4. Verify free tier covers apex custom domain before cutover.

Spec: docs/superpowers/specs/2026-06-10-mintlify-migration-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

(Replace `#<mutation-docs-PR-number>` with the PR number from Task 1.)
