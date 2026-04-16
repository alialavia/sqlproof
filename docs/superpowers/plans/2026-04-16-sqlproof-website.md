# SqlProof Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a landing page + docs site for SqlProof using Astro + Starlight, hosted on GitHub Pages from the `website/` folder in the existing repo.

**Architecture:** Astro handles the custom landing page (`index.astro`); Starlight handles the `/docs` section (sidebar, search, syntax highlighting). The site is built statically and deployed to GitHub Pages via GitHub Actions on every push to `main`. All files live in `website/` inside the existing `sqlproof` repo.

**Tech Stack:** Astro 5, @astrojs/starlight, GitHub Actions (peaceiris/actions-gh-pages), GitHub Pages

---

## File Map

**Created:**
- `website/package.json` — Astro + Starlight deps
- `website/tsconfig.json` — TypeScript config for Astro
- `website/astro.config.mjs` — Starlight sidebar, GitHub Pages base URL, custom CSS
- `website/src/styles/custom.css` — Dark/green theme overrides for Starlight + landing page CSS
- `website/src/content/config.ts` — Starlight content collection schema
- `website/src/pages/index.astro` — Full landing page (custom, no Starlight layout)
- `website/src/content/docs/getting-started.md`
- `website/src/content/docs/api/sqlproof-class.md`
- `website/src/content/docs/api/check-options.md`
- `website/src/content/docs/api/table-customization.md`
- `website/src/content/docs/guides/fk-distributions.md`
- `website/src/content/docs/guides/custom-generators.md`
- `website/src/content/docs/examples/orders.md`
- `website/public/favicon.svg`
- `.github/workflows/deploy-website.yml`

**Modified:**
- `README.md` — add link to website at top

---

## Task 1: Scaffold Astro + Starlight project

**Files:**
- Create: `website/package.json`
- Create: `website/tsconfig.json`
- Create: `website/astro.config.mjs`
- Create: `website/src/content/config.ts`
- Create: `website/public/favicon.svg`

- [ ] **Step 1: Create `website/package.json`**

```json
{
  "name": "sqlproof-website",
  "type": "module",
  "version": "0.0.1",
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "preview": "astro preview"
  },
  "dependencies": {
    "@astrojs/starlight": "^0.30.0",
    "astro": "^5.0.0",
    "sharp": "^0.33.0"
  }
}
```

- [ ] **Step 2: Create `website/tsconfig.json`**

```json
{
  "extends": "astro/tsconfigs/strict",
  "include": [".astro/types.d.ts", "**/*"],
  "exclude": ["dist"]
}
```

- [ ] **Step 3: Create `website/astro.config.mjs`**

Replace `YOUR_GITHUB_USERNAME` with the actual GitHub username (e.g. if the repo is at `github.com/ali/sqlproof`, use `ali`).

```javascript
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://YOUR_GITHUB_USERNAME.github.io',
  base: '/sqlproof',
  integrations: [
    starlight({
      title: 'SqlProof',
      defaultTheme: 'dark',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/YOUR_GITHUB_USERNAME/sqlproof',
        },
      ],
      customCss: ['./src/styles/custom.css'],
      sidebar: [
        { label: 'Getting Started', slug: 'getting-started' },
        {
          label: 'API Reference',
          items: [
            { label: 'SqlProof Class', slug: 'api/sqlproof-class' },
            { label: 'CheckOptions', slug: 'api/check-options' },
            { label: 'TableCustomization', slug: 'api/table-customization' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'FK Distribution Strategies', slug: 'guides/fk-distributions' },
            { label: 'Custom Generators', slug: 'guides/custom-generators' },
          ],
        },
        {
          label: 'Examples',
          items: [{ label: 'E-Commerce Orders', slug: 'examples/orders' }],
        },
      ],
    }),
  ],
});
```

- [ ] **Step 4: Create `website/src/content/config.ts`**

```typescript
import { defineCollection } from 'astro:content';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
  docs: defineCollection({ schema: docsSchema() }),
};
```

- [ ] **Step 5: Create `website/public/favicon.svg`**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <rect width="32" height="32" rx="6" fill="#0d1f12"/>
  <text x="5" y="24" font-family="monospace" font-size="20" font-weight="bold" fill="#22c55e">S</text>
</svg>
```

- [ ] **Step 6: Install dependencies**

```bash
cd website && npm install
```

Expected: `node_modules/` created, no errors.

- [ ] **Step 7: Verify dev server starts**

Astro requires at least one page to start. Create a minimal placeholder page first:

```bash
mkdir -p website/src/pages
```

Create `website/src/pages/index.astro` with minimal content (will be replaced in Task 3):

```astro
---
---
<html><body><h1>SqlProof</h1></body></html>
```

Then run:

```bash
cd website && npm run dev
```

Expected: Server starts at `http://localhost:4321`. Visit it and see "SqlProof". Ctrl+C to stop.

- [ ] **Step 8: Commit**

```bash
git add website/
git commit -m "feat(website): scaffold Astro + Starlight project"
```

---

## Task 2: Custom visual theme (dark/green CSS)

**Files:**
- Create: `website/src/styles/custom.css`

- [ ] **Step 1: Create `website/src/styles/custom.css`**

This file overrides Starlight's CSS custom properties to apply the dark/green color palette. It also contains the shared landing page styles (used in Task 3).

```css
/* ============================================================
   Starlight theme overrides — dark/green palette
   ============================================================ */

:root,
:root[data-theme='dark'] {
  /* Accent — green */
  --sl-color-accent-low: #14291a;
  --sl-color-accent: #22c55e;
  --sl-color-accent-high: #4ade80;

  /* Background layers */
  --sl-color-bg: #0d1f12;
  --sl-color-bg-sidebar: #0a1a0e;
  --sl-color-bg-nav: #0a1a0e;
  --sl-color-bg-inline-code: #14291a;

  /* Text */
  --sl-color-white: #f0fdf4;
  --sl-color-gray-1: #f0fdf4;
  --sl-color-gray-2: #d1fae5;
  --sl-color-gray-3: #86efac;
  --sl-color-gray-4: #4ade80;
  --sl-color-gray-5: #22c55e33;
  --sl-color-gray-6: #14291a;
  --sl-color-black: #0d1f12;

  /* Hairlines */
  --sl-color-hairline: #22c55e1a;
  --sl-color-hairline-light: #22c55e0f;
  --sl-color-hairline-shade: #22c55e33;
}

/* Hide theme toggle — we're dark-only */
starlight-theme-select {
  display: none;
}

/* ============================================================
   Landing page styles (used by index.astro)
   ============================================================ */

.sp-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 18px 48px;
  border-bottom: 1px solid #22c55e1a;
}
.sp-logo {
  font-family: monospace;
  font-size: 18px;
  font-weight: 700;
  color: #22c55e;
  text-decoration: none;
}
.sp-nav-links {
  display: flex;
  gap: 28px;
  align-items: center;
}
.sp-nav-links a {
  color: #86efac;
  text-decoration: none;
  font-size: 14px;
}
.sp-nav-links a:hover { color: #22c55e; }
.sp-nav-cta {
  background: #22c55e;
  color: #0d1f12 !important;
  padding: 7px 16px;
  border-radius: 5px;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
}

.sp-hero {
  text-align: center;
  padding: 100px 48px 80px;
  max-width: 760px;
  margin: 0 auto;
}
.sp-badge {
  display: inline-block;
  background: #22c55e18;
  border: 1px solid #22c55e33;
  color: #4ade80;
  padding: 4px 12px;
  border-radius: 99px;
  font-size: 12px;
  font-family: monospace;
  margin-bottom: 24px;
}
.sp-hero h1 {
  font-size: 52px;
  font-weight: 800;
  line-height: 1.1;
  color: #f0fdf4;
  margin-bottom: 20px;
  letter-spacing: -1px;
}
.sp-hero h1 span { color: #22c55e; }
.sp-hero p {
  font-size: 18px;
  color: #86efac;
  margin-bottom: 36px;
  max-width: 560px;
  margin-left: auto;
  margin-right: auto;
}
.sp-install {
  background: #14291a;
  border: 1px solid #22c55e33;
  border-radius: 8px;
  padding: 14px 24px;
  display: inline-flex;
  align-items: center;
  gap: 12px;
  font-family: monospace;
  font-size: 15px;
  color: #f0fdf4;
  margin-bottom: 20px;
}
.sp-install .prompt { color: #22c55e; }
.sp-hero-buttons {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-top: 8px;
}
.sp-btn-primary {
  background: #22c55e;
  color: #0d1f12;
  padding: 10px 24px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 700;
  text-decoration: none;
}
.sp-btn-secondary {
  border: 1px solid #22c55e44;
  color: #4ade80;
  padding: 10px 24px;
  border-radius: 6px;
  font-size: 14px;
  text-decoration: none;
}

.sp-section {
  padding: 72px 48px;
  max-width: 900px;
  margin: 0 auto;
}
.sp-section-label {
  font-size: 12px;
  font-family: monospace;
  color: #22c55e;
  text-transform: uppercase;
  letter-spacing: 2px;
  margin-bottom: 12px;
}
.sp-section h2 {
  font-size: 32px;
  font-weight: 700;
  color: #f0fdf4;
  margin-bottom: 16px;
}
.sp-section .sp-lead {
  font-size: 17px;
  color: #86efac;
  max-width: 600px;
}
.sp-why-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-top: 40px;
}
.sp-why-card {
  background: #14291a;
  border: 1px solid #22c55e1a;
  border-radius: 10px;
  padding: 24px;
}
.sp-why-card h3 {
  color: #22c55e;
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 8px;
}
.sp-why-card p { color: #86efac; font-size: 14px; }
.sp-why-card code {
  color: #22c55e;
  font-size: 12px;
  background: #0a1a0e;
  padding: 1px 4px;
  border-radius: 3px;
}

.sp-how-section {
  padding: 72px 48px;
  background: #0a1a0e;
  border-top: 1px solid #22c55e1a;
  border-bottom: 1px solid #22c55e1a;
}
.sp-how-inner { max-width: 900px; margin: 0 auto; }
.sp-steps {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 24px;
  margin-top: 40px;
}
.sp-step { text-align: center; }
.sp-step-num {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: #22c55e1a;
  border: 1px solid #22c55e44;
  color: #22c55e;
  font-family: monospace;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto 12px;
}
.sp-step h3 { font-size: 14px; font-weight: 600; color: #f0fdf4; margin-bottom: 6px; }
.sp-step p { font-size: 13px; color: #86efac; }

.sp-code-section { padding: 72px 48px; max-width: 900px; margin: 0 auto; }
.sp-code-block {
  background: #0a1a0e;
  border: 1px solid #22c55e22;
  border-radius: 10px;
  overflow: hidden;
  margin-top: 32px;
}
.sp-code-header {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid #22c55e1a;
  align-items: center;
}
.sp-code-dot { width: 10px; height: 10px; border-radius: 50%; }
.sp-code-filename { font-family: monospace; font-size: 12px; color: #4ade80; margin-left: 8px; }
.sp-code-body {
  padding: 20px 24px;
  font-family: monospace;
  font-size: 13px;
  line-height: 1.8;
  overflow-x: auto;
  white-space: pre;
}
.sp-kw { color: #4ade80; }
.sp-fn { color: #86efac; }
.sp-str { color: #fde68a; }
.sp-cm { color: #3d6b4a; }
.sp-num { color: #f9a8d4; }

.sp-divider { border: none; border-top: 1px solid #22c55e1a; margin: 0; }

.sp-footer {
  padding: 40px 48px;
  border-top: 1px solid #22c55e1a;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.sp-footer-logo { font-family: monospace; color: #22c55e; font-weight: 700; }
.sp-footer-sub { color: #3d6b4a; font-size: 13px; margin-top: 4px; }
.sp-footer-links { display: flex; gap: 24px; }
.sp-footer-links a { color: #4ade80; text-decoration: none; font-size: 13px; }

@media (max-width: 768px) {
  .sp-nav { padding: 16px 24px; }
  .sp-nav-links { display: none; }
  .sp-hero { padding: 60px 24px 48px; }
  .sp-hero h1 { font-size: 36px; }
  .sp-section { padding: 48px 24px; }
  .sp-why-grid { grid-template-columns: 1fr; }
  .sp-how-section { padding: 48px 24px; }
  .sp-steps { grid-template-columns: 1fr 1fr; }
  .sp-code-section { padding: 48px 24px; }
  .sp-footer { padding: 32px 24px; flex-direction: column; gap: 20px; text-align: center; }
}
```

- [ ] **Step 2: Verify styles are referenced in `astro.config.mjs`**

Confirm `customCss: ['./src/styles/custom.css']` is present in `astro.config.mjs` (it was set in Task 1).

- [ ] **Step 3: Commit**

```bash
git add website/src/styles/custom.css
git commit -m "feat(website): add dark/green theme CSS"
```

---

## Task 3: Build the landing page

**Files:**
- Modify: `website/src/pages/index.astro` (replace the placeholder from Task 1)

- [ ] **Step 1: Write `website/src/pages/index.astro`**

Replace the entire file with:

```astro
---
const base = import.meta.env.BASE_URL;
// base is '/sqlproof/' in production (GitHub Pages), '/' in dev
const docsBase = `${base}getting-started/`;
const apiBase = `${base}api/sqlproof-class/`;
const examplesBase = `${base}examples/orders/`;
const githubUrl = 'https://github.com/YOUR_GITHUB_USERNAME/sqlproof';
---

<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>SqlProof — Property-based testing for PostgreSQL</title>
    <meta name="description" content="Automatically generate valid test data that respects your schema constraints, then find the minimal counterexample when your queries break." />
    <link rel="icon" type="image/svg+xml" href={`${base}favicon.svg`} />
    <link rel="stylesheet" href={`${base}_astro/custom.css`} />
    <style>
      *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
      html { scroll-behavior: smooth; }
      body {
        background: #0d1f12;
        color: #f0fdf4;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        line-height: 1.6;
      }
    </style>
    <link rel="stylesheet" href="/sqlproof/styles/landing.css" />
  </head>
  <body>

    <!-- Nav -->
    <nav class="sp-nav">
      <a href={base} class="sp-logo">SqlProof</a>
      <div class="sp-nav-links">
        <a href={docsBase}>Docs</a>
        <a href={apiBase}>API</a>
        <a href={examplesBase}>Examples</a>
        <a href={githubUrl} target="_blank" rel="noopener">GitHub ↗</a>
        <a href={docsBase} class="sp-nav-cta">Get Started</a>
      </div>
    </nav>

    <!-- Hero -->
    <div class="sp-hero">
      <div class="sp-badge">v0.1.0 — now on npm</div>
      <h1>Find SQL bugs<br />before your <span>users</span> do</h1>
      <p>
        Property-based testing for PostgreSQL. Generates random valid datasets
        that respect your schema — then tries to break your queries.
      </p>
      <div class="sp-install">
        <span class="prompt">$</span>
        <span>npm install sqlproof</span>
      </div>
      <div class="sp-hero-buttons">
        <a href={docsBase} class="sp-btn-primary">Get Started →</a>
        <a href={githubUrl} target="_blank" rel="noopener" class="sp-btn-secondary">View on GitHub</a>
      </div>
    </div>

    <hr class="sp-divider" />

    <!-- Why SqlProof -->
    <div class="sp-section">
      <div class="sp-section-label">// why sqlproof</div>
      <h2>Your queries work on your fixtures.<br />Do they work on real data?</h2>
      <p class="sp-lead">
        Hand-crafted test data misses edge cases. SqlProof generates thousands of valid
        random datasets and finds the one that breaks your invariants.
      </p>
      <div class="sp-why-grid">
        <div class="sp-why-card">
          <h3>Schema-aware generation</h3>
          <p>Respects foreign keys, CHECK constraints, UNIQUE, NOT NULL, and enum types automatically. No invalid data, no constraint violations.</p>
        </div>
        <div class="sp-why-card">
          <h3>Minimal counterexamples</h3>
          <p>When a property fails, fast-check shrinks the dataset to the smallest possible example — so you fix the bug, not hunt for it.</p>
        </div>
        <div class="sp-why-card">
          <h3>Zero infrastructure</h3>
          <p>Spins up a disposable Postgres via testcontainers. No external DB needed. Each run gets its own isolated schema — fast and clean.</p>
        </div>
        <div class="sp-why-card">
          <h3>Works with your test runner</h3>
          <p>Drop it into Vitest or Jest. Call <code>proof.check()</code> from an <code>it()</code> block. No new tools to learn.</p>
        </div>
      </div>
    </div>

    <!-- How it works -->
    <div class="sp-how-section">
      <div class="sp-how-inner">
        <div class="sp-section-label">// how it works</div>
        <h2 style="font-size:32px;font-weight:700;color:#f0fdf4;margin-bottom:0;">Four steps, zero boilerplate</h2>
        <div class="sp-steps">
          <div class="sp-step">
            <div class="sp-step-num">1</div>
            <h3>Parse schema</h3>
            <p>Reads your SQL file or introspects a live Postgres DB to extract tables, types, FKs, and constraints.</p>
          </div>
          <div class="sp-step">
            <div class="sp-step-num">2</div>
            <h3>Generate data</h3>
            <p>Creates random valid rows for every table in FK-dependency order. Respects all constraints.</p>
          </div>
          <div class="sp-step">
            <div class="sp-step-num">3</div>
            <h3>Insert &amp; run</h3>
            <p>Inserts into an isolated Postgres schema, runs your property, then drops the schema. Repeats up to N times.</p>
          </div>
          <div class="sp-step">
            <div class="sp-step-num">4</div>
            <h3>Report</h3>
            <p>On failure, shrinks to the minimal counterexample and reports it with a reproducible seed.</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Code example -->
    <div class="sp-code-section">
      <div class="sp-section-label">// example</div>
      <h2 style="font-size:32px;font-weight:700;color:#f0fdf4;">See it in action</h2>
      <div class="sp-code-block">
        <div class="sp-code-header">
          <div class="sp-code-dot" style="background:#ff5f57"></div>
          <div class="sp-code-dot" style="background:#febc2e"></div>
          <div class="sp-code-dot" style="background:#28c840"></div>
          <span class="sp-code-filename">orders.test.ts</span>
        </div>
        <div class="sp-code-body"><span class="sp-kw">import</span> &#123; SqlProof &#125; <span class="sp-kw">from</span> <span class="sp-str">'sqlproof'</span>;

<span class="sp-kw">const</span> proof = <span class="sp-kw">await</span> SqlProof.<span class="sp-fn">connect</span>(&#123; schemaFile: <span class="sp-str">'./schema.sql'</span> &#125;);

<span class="sp-kw">await</span> proof.<span class="sp-fn">invariant</span>(<span class="sp-str">'no orphan line items'</span>, &#123;
  generate: &#123; customers: <span class="sp-num">10</span>, orders: <span class="sp-num">50</span>, line_items: <span class="sp-num">200</span> &#125;,
  query: <span class="sp-str">`SELECT li.id FROM line_items li
         LEFT JOIN orders o ON li.order_id = o.id
         WHERE o.id IS NULL`</span>,
  expectEmpty: <span class="sp-kw">true</span>,
&#125;);

<span class="sp-cm">// ✗ Property failed: "no orphan line items"</span>
<span class="sp-cm">// After 3 runs — seed: 1708891234</span>
<span class="sp-cm">// Reproduce: proof.invariant('...', &#123; ..., seed: 1708891234 &#125;)</span></div>
      </div>
    </div>

    <!-- Footer -->
    <footer class="sp-footer">
      <div>
        <div class="sp-footer-logo">SqlProof</div>
        <div class="sp-footer-sub">MIT License · Built on fast-check, pg, testcontainers</div>
      </div>
      <div class="sp-footer-links">
        <a href={docsBase}>Docs</a>
        <a href={githubUrl} target="_blank" rel="noopener">GitHub</a>
        <a href="https://www.npmjs.com/package/sqlproof" target="_blank" rel="noopener">npm</a>
      </div>
    </footer>

  </body>
</html>
```

> **Note:** The `<link rel="stylesheet">` for landing.css won't work with Astro's asset pipeline. Instead, move all the CSS from `custom.css` that's prefixed `.sp-` directly into a `<style>` tag in this file, OR import the custom.css file with `import '../styles/custom.css'` in the frontmatter (Astro will bundle it). Use the import approach:

Replace the two `<link rel="stylesheet">` tags in `<head>` with nothing — and add this to the frontmatter:

```astro
---
import '../styles/custom.css';
const base = import.meta.env.BASE_URL;
// ... rest of frontmatter
---
```

- [ ] **Step 2: Run dev server and verify landing page**

```bash
cd website && npm run dev
```

Open `http://localhost:4321`. Verify:
- Dark green background
- Nav with logo and links
- Hero with headline, install command, buttons
- Why section with 4 cards
- How it works with 4 steps
- Code example block
- Footer

- [ ] **Step 3: Commit**

```bash
git add website/src/pages/index.astro
git commit -m "feat(website): add landing page"
```

---

## Task 4: Getting Started doc

**Files:**
- Create: `website/src/content/docs/getting-started.md`

- [ ] **Step 1: Create `website/src/content/docs/getting-started.md`**

```markdown
---
title: Getting Started
description: Install SqlProof and write your first property test in minutes.
---

SqlProof is a property-based testing library for PostgreSQL. It generates random valid datasets that respect your schema constraints, runs your properties against them, and reports the minimal counterexample when one fails.

## Prerequisites

- Node.js 18+
- PostgreSQL 13+ (or Docker, if using testcontainers)

## Install

```bash
npm install sqlproof
```

For automatic disposable Postgres instances (no external DB required):

```bash
npm install -D @testcontainers/postgresql
```

If using testcontainers, Docker must be running.

## Quick Start

Given a schema file:

```sql
-- schema.sql
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
);
```

Write property tests with Vitest (or Jest):

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

describe('order queries', () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile: './schema.sql' });
  }, 120_000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('every order has a valid customer', async () => {
    await proof.invariant('no orphan orders', {
      generate: { customers: 10, orders: 50 },
      query: `
        SELECT o.id FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE c.id IS NULL
      `,
      expectEmpty: true,
      runs: 50,
    });
  });
});
```

## Connecting to an Existing Database

If you have a running Postgres instance, pass a connection string instead of a schema file:

```typescript
const proof = await SqlProof.connect({
  connectionString: 'postgresql://localhost:5432/mydb',
  schema: 'public', // optional, defaults to 'public'
});
```

SqlProof will introspect your live database schema and use it for data generation.

## Vitest Configuration

Add `pool: 'forks'` to your Vitest config — required for testcontainers compatibility:

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    pool: 'forks',
  },
});
```

## What Happens Under the Hood

1. **Schema parsing** — reads your `.sql` file (or introspects the DB) to extract tables, columns, FKs, CHECK/UNIQUE constraints, and enum types
2. **Topological sort** — orders tables by FK dependencies so parent rows are always inserted before children
3. **Data generation** — maps Postgres types to [fast-check](https://github.com/dubzzz/fast-check) arbitraries and applies constraint-aware generation
4. **Schema isolation** — each run creates `CREATE SCHEMA run_<uuid>`, inserts data, runs your property, then `DROP SCHEMA CASCADE`
5. **Shrinking** — when a property fails, fast-check shrinks the dataset to the smallest counterexample and reports it with a reproducible seed
```

- [ ] **Step 2: Verify in dev server**

```bash
cd website && npm run dev
```

Visit `http://localhost:4321/getting-started/`. Verify the page renders with sidebar navigation.

- [ ] **Step 3: Commit**

```bash
git add website/src/content/docs/getting-started.md
git commit -m "docs(website): add Getting Started page"
```

---

## Task 5: API Reference — SqlProof Class

**Files:**
- Create: `website/src/content/docs/api/sqlproof-class.md`
- Create: `website/src/content/docs/api/check-options.md`
- Create: `website/src/content/docs/api/table-customization.md`

- [ ] **Step 1: Create `website/src/content/docs/api/sqlproof-class.md`**

```markdown
---
title: SqlProof Class
description: The main class for connecting to Postgres and running property tests.
---

The `SqlProof` class is the entry point for all property tests. Create one instance per test suite via `SqlProof.connect()`, share it across all `check()` and `invariant()` calls, then call `disconnect()` in cleanup.

## `SqlProof.connect(options)`

Factory method. Connects to Postgres (or starts a testcontainers instance), introspects the schema, and returns a ready `SqlProof` instance.

```typescript
static async connect(options: SqlProofConnectOptions): Promise<SqlProof>
```

**Options:**

| Field | Type | Description |
|---|---|---|
| `schemaFile` | `string` | Path to a `.sql` DDL file. Auto-starts a testcontainers Postgres. |
| `connectionString` | `string` | `postgresql://` URL. Connects to an existing Postgres instance. |
| `schema` | `string` | Postgres schema name to introspect. Default: `'public'`. Only used with `connectionString`. |

Exactly one of `schemaFile` or `connectionString` must be provided. Throws if both or neither are given.

**Example:**

```typescript
// With a SQL file (auto-manages Postgres via testcontainers):
const proof = await SqlProof.connect({ schemaFile: './schema.sql' });

// With an existing database:
const proof = await SqlProof.connect({
  connectionString: 'postgresql://localhost:5432/mydb',
});
```

---

## `proof.customize(table, overrides)`

Register custom generators or FK distribution strategies for a table. Returns `this` for fluent chaining.

```typescript
customize(table: string, overrides: TableCustomization): this
```

Must be called before `check()` or `invariant()`. See [TableCustomization](/api/table-customization/) for the full options.

**Example:**

```typescript
import fc from 'fast-check';

proof
  .customize('products', {
    price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
    name: fc.string({ minLength: 1, maxLength: 100 }),
  })
  .customize('orders', {
    fkDistribution: { customer_id: 'zipf' },
  });
```

---

## `proof.check(name, options)`

Run a property-based test. Throws `SqlProofError` on failure with a formatted counterexample including a reproducible seed.

```typescript
async check(name: string, options: CheckOptions): Promise<void>
```

See [CheckOptions](/api/check-options/) for the full options reference.

**Example:**

```typescript
await proof.check('order totals are non-negative', {
  generate: { customers: 10, orders: 50, line_items: 200 },
  property: async (db) => {
    const result = await db.query('SELECT total FROM orders');
    return result.rows.every(row => Number(row.total) >= 0);
  },
  runs: 100,
});
```

---

## `proof.invariant(name, options)`

Declarative shorthand: asserts that a SQL query returns zero rows for all generated datasets. Thin wrapper over `check()`.

```typescript
async invariant(name: string, options: InvariantOptions): Promise<void>
```

| Field | Type | Description |
|---|---|---|
| `generate` | `Record<string, number>` | Per-table row counts. |
| `query` | `string` | SQL query. Must return 0 rows for the invariant to hold. |
| `expectEmpty` | `true` | Always `true` — makes the intent explicit. |
| `runs` | `number` | Number of datasets to test. Default: `100`. |
| `seed` | `number` | Reproduce a specific failure. |
| `timeout` | `number` | Per-run timeout in ms. Default: `5000`. |

**Example:**

```typescript
await proof.invariant('no orphan line items', {
  generate: { customers: 10, orders: 50, line_items: 200 },
  query: `
    SELECT li.id FROM line_items li
    LEFT JOIN orders o ON li.order_id = o.id
    WHERE o.id IS NULL
  `,
  expectEmpty: true,
  runs: 50,
});
```

---

## `proof.disconnect()`

Close the Postgres connection and stop the testcontainers instance (if auto-managed). Call in `afterEach` or `afterAll`.

```typescript
async disconnect(): Promise<void>
```

**Example:**

```typescript
afterEach(async () => {
  await proof?.disconnect();
});
```
```

- [ ] **Step 2: Create `website/src/content/docs/api/check-options.md`**

```markdown
---
title: CheckOptions
description: Options for proof.check() — per-table row counts, property function, and test configuration.
---

`CheckOptions` is passed as the second argument to `proof.check()`.

```typescript
interface CheckOptions {
  generate: Record<string, number>;
  setup?: (db: SqlProofClient) => Promise<void>;
  property: (db: SqlProofClient) => Promise<boolean>;
  runs?: number;
  seed?: number;
  timeout?: number;
}
```

## Fields

### `generate` (required)

Per-table row counts. Keys are table names; values are the number of rows to generate per run.

```typescript
generate: { customers: 20, orders: 100, line_items: 500 }
```

Only tables listed here will have data generated. Tables not listed will be empty.

### `property` (required)

A function that receives a `SqlProofClient` and returns `Promise<boolean>`. Return `true` if the property holds, `false` if it is violated. Throwing also counts as a violation.

```typescript
property: async (db) => {
  const result = await db.query('SELECT total FROM orders WHERE total < 0');
  return result.rows.length === 0;
}
```

### `setup` (optional)

A function that runs after data insertion but before the property check. Use it for mutations or additional setup that depend on the inserted data.

```typescript
setup: async (db) => {
  await db.query(`UPDATE orders SET status = 'confirmed' WHERE total > 100`);
}
```

### `runs` (optional)

Number of random datasets to generate and test. Default: `100`.

### `seed` (optional)

Integer seed for deterministic data generation. Use the seed reported in a failure to reproduce the exact counterexample:

```typescript
await proof.check('order totals are non-negative', {
  generate: { customers: 10, orders: 50 },
  property: async (db) => { /* ... */ },
  seed: 1708891234, // from failure output
});
```

### `timeout` (optional)

Per-run timeout in milliseconds. Default: `5000`. Increase for slow properties or large datasets.

## `SqlProofClient`

The `db` object passed to `property` and `setup`:

```typescript
interface SqlProofClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  getGeneratedData(): Dataset;
}
```

- `query()` — runs SQL against the isolated test schema for the current run
- `getGeneratedData()` — returns the full inserted dataset (useful for debugging)
```

- [ ] **Step 3: Create `website/src/content/docs/api/table-customization.md`**

```markdown
---
title: TableCustomization
description: Override column generators and FK distribution strategies per table.
---

`TableCustomization` is passed to `proof.customize(table, overrides)`. It lets you replace default column generators with custom fast-check arbitraries and control how foreign key values are distributed.

```typescript
interface TableCustomization {
  fkDistribution?: Record<string, FkDistributionStrategy>;
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';
```

## Custom Column Generators

Override the default generator for any column by passing a [fast-check](https://github.com/dubzzz/fast-check) arbitrary:

```typescript
import fc from 'fast-check';

proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
  sku: fc.stringMatching(/^[A-Z]{2}-\d{4}$/),
});
```

Custom generators override SqlProof's default type-based generators for those columns. All other columns continue to use defaults.

## FK Distribution Strategies

Control how foreign key values are assigned when referencing parent rows:

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});

proof.customize('line_items', {
  fkDistribution: {
    order_id: 'zipf',
    product_id: 'adversarial',
  },
});
```

| Strategy | Behavior | Best for |
|---|---|---|
| `uniform` (default) | Each parent row has equal probability of being picked | General coverage |
| `zipf` | First parents get many children; later ones few or none | Realistic skewed data (hot customers, popular products) |
| `adversarial` | Only picks the first, middle, and last parent rows | Boundary stress testing |

See the [FK Distribution Strategies guide](/guides/fk-distributions/) for more detail.

## Fluent Chaining

`customize()` returns `this`, so calls can be chained:

```typescript
proof
  .customize('products', { price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }) })
  .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
  .customize('line_items', { fkDistribution: { order_id: 'zipf', product_id: 'adversarial' } });
```

Multiple calls to `customize()` for the same table are merged — later calls add to (not replace) earlier customizations.
```

- [ ] **Step 4: Verify in dev server**

```bash
cd website && npm run dev
```

Visit `http://localhost:4321/api/sqlproof-class/`, `/api/check-options/`, and `/api/table-customization/`. Verify all three pages render with correct sidebar links.

- [ ] **Step 5: Commit**

```bash
git add website/src/content/docs/api/
git commit -m "docs(website): add API reference pages"
```

---

## Task 6: Guides

**Files:**
- Create: `website/src/content/docs/guides/fk-distributions.md`
- Create: `website/src/content/docs/guides/custom-generators.md`

- [ ] **Step 1: Create `website/src/content/docs/guides/fk-distributions.md`**

```markdown
---
title: FK Distribution Strategies
description: Control how foreign key references are distributed across parent rows.
---

By default, when SqlProof generates a child row that references a parent table, it picks uniformly at random from all available parent rows. FK distribution strategies let you change this behavior to simulate more realistic or adversarial data patterns.

## Why It Matters

Real-world databases are rarely uniform. A small set of customers places the majority of orders. A handful of products get most of the line items. If your queries have performance or correctness issues under skewed load, uniform random data might never surface them.

## Available Strategies

### `uniform` (default)

Each parent row has equal probability of being referenced. Good for general coverage.

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'uniform' },
});
```

### `zipf`

References are skewed: the first parent row is referenced most often, the second less so, and so on, following a Zipf distribution (weight ∝ 1/(rank+1)).

Use this to simulate realistic skewed data — a few popular customers, products, or categories getting the bulk of activity.

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});
```

With 5 parent rows, approximate pick probabilities:
- Row 1: ~44%
- Row 2: ~22%
- Row 3: ~15%
- Row 4: ~11%
- Row 5: ~7%

### `adversarial`

Only picks from the first, middle, and last parent rows. Useful for boundary stress tests — do your queries behave correctly at the edges of a dataset?

```typescript
proof.customize('line_items', {
  fkDistribution: { product_id: 'adversarial' },
});
```

## Combining Strategies

Different columns in the same table can use different strategies:

```typescript
proof.customize('line_items', {
  fkDistribution: {
    order_id: 'zipf',       // skewed — a few orders get many items
    product_id: 'adversarial', // boundary test for products
  },
});
```

## Full Example

```typescript
const proof = await SqlProof.connect({ schemaFile: './schema.sql' });

proof
  .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
  .customize('line_items', {
    fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
  });

await proof.check('FK integrity holds under skewed load', {
  generate: { customers: 5, orders: 20, products: 5, line_items: 100 },
  property: async (db) => {
    const orphans = await db.query(`
      SELECT li.id FROM line_items li
      LEFT JOIN orders o ON li.order_id = o.id
      WHERE o.id IS NULL
    `);
    return orphans.rows.length === 0;
  },
  runs: 50,
});

await proof.disconnect();
```
```

- [ ] **Step 2: Create `website/src/content/docs/guides/custom-generators.md`**

```markdown
---
title: Custom Generators
description: Override default column generators with fast-check arbitraries.
---

SqlProof maps PostgreSQL types to [fast-check](https://github.com/dubzzz/fast-check) arbitraries automatically. For most columns the defaults work fine, but sometimes you need tighter control — realistic email addresses, specific numeric ranges, or domain-constrained strings.

## Basic Override

Pass a fast-check arbitrary for any column via `proof.customize()`:

```typescript
import fc from 'fast-check';

proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
});
```

The arbitrary you provide completely replaces SqlProof's default generator for that column. All constraints (nullability, etc.) are still applied on top.

## Common Patterns

### Numeric ranges

```typescript
proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  stock: fc.integer({ min: 0, max: 10000 }),
  discount_pct: fc.float({ min: 0, max: 0.5, noNaN: true }),
});
```

### Constrained strings

```typescript
proof.customize('customers', {
  email: fc.emailAddress(),
  name: fc.string({ minLength: 2, maxLength: 100 }),
  phone: fc.stringMatching(/^\+1-\d{3}-\d{3}-\d{4}$/),
});
```

### Picking from a fixed set

```typescript
proof.customize('orders', {
  currency: fc.constantFrom('USD', 'EUR', 'GBP'),
  region: fc.constantFrom('us-east', 'us-west', 'eu-central'),
});
```

### Dates in a specific range

```typescript
proof.customize('orders', {
  created_at: fc.date({
    min: new Date('2020-01-01'),
    max: new Date('2024-12-31'),
    noInvalidDate: true,
  }),
});
```

## Default Type Mappings

SqlProof uses these defaults when no override is provided:

| PostgreSQL Type | Default Arbitrary |
|---|---|
| `integer`, `int4` | `fc.integer({ min: -2147483648, max: 2147483647 })` |
| `bigint` | `fc.bigInt()` |
| `smallint` | `fc.integer({ min: -32768, max: 32767 })` |
| `numeric(p,s)`, `decimal` | `fc.float()` (scaled to precision) |
| `real`, `float4` | `fc.float({ noNaN: true, noDefaultInfinity: true })` |
| `double precision` | `fc.double({ noNaN: true, noDefaultInfinity: true })` |
| `boolean` | `fc.boolean()` |
| `text` | `fc.string({ unit: 'grapheme', maxLength: 255 })` |
| `varchar(n)` | `fc.string({ unit: 'grapheme', maxLength: n })` |
| `uuid` | `fc.uuid()` |
| `timestamp`, `timestamptz` | `fc.date({ noInvalidDate: true, min: 1970, max: 2099 })` |
| `date` | `fc.date()` formatted as `YYYY-MM-DD` |
| `json`, `jsonb` | `fc.jsonValue()` |
| `enum` types | `fc.constantFrom(...enumValues)` |
| `integer[]`, etc. | `fc.array()` of base type |
```

- [ ] **Step 3: Verify in dev server**

```bash
cd website && npm run dev
```

Visit `http://localhost:4321/guides/fk-distributions/` and `/guides/custom-generators/`. Verify both pages render.

- [ ] **Step 4: Commit**

```bash
git add website/src/content/docs/guides/
git commit -m "docs(website): add guides for FK distributions and custom generators"
```

---

## Task 7: Examples doc

**Files:**
- Create: `website/src/content/docs/examples/orders.md`

- [ ] **Step 1: Create `website/src/content/docs/examples/orders.md`**

```markdown
---
title: E-Commerce Orders
description: A complete walkthrough using SqlProof with a realistic e-commerce schema.
---

This example walks through using SqlProof with a realistic e-commerce schema that has four tables, foreign keys, CHECK constraints, and an enum type.

## The Schema

```sql
CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'shipped', 'delivered', 'cancelled');

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  price NUMERIC(10,2) NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status order_status NOT NULL DEFAULT 'pending',
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE line_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  price NUMERIC(10,2) NOT NULL CHECK (price > 0)
);
```

## Test File

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';
import fc from 'fast-check';

const schemaFile = new URL('./schema.sql', import.meta.url).pathname;

describe('e-commerce properties', { timeout: 120_000 }, () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile });
  }, 120_000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('order totals are always non-negative', async () => {
    await proof.check('order totals are non-negative', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      property: async (db) => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row.total) >= 0);
      },
      runs: 50,
    });
  });

  it('every line item references a valid order', async () => {
    await proof.invariant('no orphan line items', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 50,
    });
  });

  it('order total equals sum of line item costs (demonstrates a failing property)', async () => {
    // This property will fail — orders.total is generated randomly,
    // not computed from line_items. Demonstrates counterexample output.
    try {
      await proof.check('order totals match line items', {
        generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
        property: async (db) => {
          const result = await db.query(`
            SELECT
              o.total as stored_total,
              COALESCE(SUM(li.price * li.quantity), 0) as computed_total
            FROM orders o
            LEFT JOIN line_items li ON o.id = li.order_id
            GROUP BY o.id, o.total
          `);
          return result.rows.every(
            row => Math.abs(Number(row.stored_total) - Number(row.computed_total)) < 0.01,
          );
        },
        runs: 50,
      });
    } catch (err) {
      console.log(err.message); // prints the counterexample table
    }
  });

  it('FK integrity holds under zipf distribution', async () => {
    proof
      .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
      .customize('line_items', {
        fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
      });

    await proof.invariant('FK integrity with skewed distribution', {
      generate: { customers: 5, orders: 20, products: 5, line_items: 50 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 20,
    });
  });
});
```

## What to Expect

When you run the tests:

- The first three tests pass — SqlProof generates data that respects the schema constraints (non-negative totals, valid FK references).
- The "order total equals sum of line items" test **intentionally fails** to demonstrate the counterexample output:

```
✗ Property failed: "order totals match line items"

  After 1 run(s) (seed: 1708891234)

  Counterexample (shrunk 4 time(s)):

  Table: orders
  ┌────┬────────┐
  │ id │ total  │
  ├────┼────────┤
  │ 1  │ 100.00 │
  └────┴────────┘

  Table: line_items
  ┌────┬──────────┬───────┬──────────┐
  │ id │ order_id │ price │ quantity │
  ├────┼──────────┼───────┼──────────┤
  │ 1  │ 1        │ 30.00 │ 2        │
  └────┴──────────┴───────┴──────────┘

  Reproduce: proof.check('...', { ..., seed: 1708891234 })
```

The source code for this example lives in `examples/orders/` in the repository.
```

- [ ] **Step 2: Verify in dev server**

```bash
cd website && npm run dev
```

Visit `http://localhost:4321/examples/orders/`. Verify the page renders with correct code blocks.

- [ ] **Step 3: Commit**

```bash
git add website/src/content/docs/examples/orders.md
git commit -m "docs(website): add e-commerce orders example"
```

---

## Task 8: GitHub Actions deploy workflow

**Files:**
- Create: `.github/workflows/deploy-website.yml`

- [ ] **Step 1: Enable GitHub Pages on the repo**

In GitHub → repo Settings → Pages:
- Set **Source** to `Deploy from a branch`
- Set **Branch** to `gh-pages`, folder `/ (root)`
- Save

(The branch will be created automatically by the workflow on first run.)

- [ ] **Step 2: Create `.github/workflows/deploy-website.yml`**

```yaml
name: Deploy website to GitHub Pages

on:
  push:
    branches: [main]
    paths:
      - 'website/**'
      - '.github/workflows/deploy-website.yml'

  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Node
        uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: website/package-lock.json

      - name: Install dependencies
        run: npm ci
        working-directory: website

      - name: Build
        run: npm run build
        working-directory: website

      - name: Deploy to GitHub Pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: website/dist
```

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/deploy-website.yml
git commit -m "ci: add GitHub Actions workflow to deploy website to GitHub Pages"
git push origin main
```

- [ ] **Step 4: Verify the workflow runs**

In GitHub → Actions tab, verify the "Deploy website to GitHub Pages" workflow runs and passes. After it completes, the site will be live at `https://YOUR_GITHUB_USERNAME.github.io/sqlproof/`.

---

## Task 9: Update root README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add website link at the top of `README.md`**

Open `README.md`. After the `# SqlProof` heading and before the description paragraph, add:

```markdown
**→ Full docs: [YOUR_GITHUB_USERNAME.github.io/sqlproof](https://YOUR_GITHUB_USERNAME.github.io/sqlproof)**
```

Also update the Quick Start code example (currently uses the old `sqlproof.check()` flat API) to match the new class-based API:

Replace the Quick Start section's TypeScript example with:

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

describe('order queries', () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile: './schema.sql' });
  }, 120_000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('every line item references a valid order', async () => {
    await proof.invariant('no orphan line items', {
      generate: { customers: 5, orders: 20, line_items: 50 },
      query: `
        SELECT li.id
        FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 50,
    });
  });
});
```

Also update the API section to show the class-based API (remove the `sqlproof.check()` flat options table and replace with a reference to the website docs).

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README with website link and class-based API"
git push origin main
```

---

## Self-Review Checklist

- [x] **Spec §5 (site structure):** All files in the file map above — covered across tasks 1–9
- [x] **Spec §6 (landing page sections):** All 6 sections (nav, hero, why, how, code, footer) — Task 3
- [x] **Spec §7 (docs section + sidebar):** All 7 doc pages with correct sidebar — Tasks 4–7
- [x] **Spec §4 (visual identity):** CSS variables and landing page styles — Task 2
- [x] **Spec §8 (deployment):** GitHub Actions workflow — Task 8
- [x] **Spec §9 (README update):** README updated — Task 9
- [x] **No placeholders** — all code blocks are complete
- [x] **`YOUR_GITHUB_USERNAME` noted** — implementer must replace in `astro.config.mjs`, workflow, and README
