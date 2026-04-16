# SqlProof Website — Design Document

**Date:** 2026-04-16
**Status:** Approved
**Author:** Ali Alavi

---

## 1. Overview

Build a documentation + marketing website for SqlProof. The site lives in a `website/` folder inside the existing `sqlproof` repo, deploys automatically to GitHub Pages on every push to `main`.

---

## 2. Goals

- Give SqlProof a professional web presence with a landing page that explains what it does and why
- Provide a complete, searchable docs section for the API reference and guides
- Zero additional hosting cost (GitHub Pages)
- Easy to maintain — docs content is Markdown, landing page is a single Astro component

---

## 3. Tech Stack

| Concern | Choice | Reason |
|---|---|---|
| Framework | Astro + Starlight | Astro gives full landing page freedom; Starlight handles docs (sidebar, search, syntax highlighting) |
| Hosting | GitHub Pages | Free, auto-deploys from the same repo |
| Deploy | GitHub Actions | On push to `main`: build `website/`, publish `dist/` to `gh-pages` branch |
| Styling | Custom CSS on landing page; Starlight default theme (dark/green customized) | Consistent with the dark/green visual identity |

---

## 4. Visual Identity

- **Background:** `#0d1f12` (dark green-black)
- **Primary accent:** `#22c55e` (green)
- **Text:** `#f0fdf4` (near-white), `#86efac` (muted green) for secondary
- **Code backgrounds:** `#0a1a0e` / `#14291a`
- **Borders:** `#22c55e1a` (subtle green tint)
- **Font:** System sans-serif for body; monospace for code and labels

---

## 5. Site Structure

```
website/
├── src/
│   ├── pages/
│   │   └── index.astro              # Landing page
│   └── content/docs/
│       ├── getting-started.md       # Install, quick start, Docker/testcontainers setup
│       ├── api/
│       │   ├── sqlproof-class.md    # connect(), check(), invariant(), customize(), disconnect()
│       │   ├── check-options.md     # CheckOptions interface: generate, property, setup, runs, seed, timeout
│       │   └── table-customization.md  # TableCustomization, FkDistributionStrategy
│       ├── guides/
│       │   ├── fk-distributions.md  # zipf, uniform, adversarial strategies
│       │   └── custom-generators.md # Using fast-check arbitraries as column overrides
│       └── examples/
│           └── orders.md            # E-commerce schema walkthrough
├── public/
│   └── favicon.svg
├── astro.config.mjs
└── package.json
```

---

## 6. Landing Page Sections

The landing page (`index.astro`) has the following sections in order:

### 6.1 Nav
- Logo: `SqlProof` (monospace, green)
- Links: Docs · API · Examples · GitHub ↗
- CTA button: `Get Started` (filled green)

### 6.2 Hero
- Badge: `v0.1.0 — now on npm`
- Headline: **"Find SQL bugs before your users do"**
- Subheadline: "Property-based testing for PostgreSQL. Generates random valid datasets that respect your schema — then tries to break your queries."
- Install command: `$ npm install sqlproof`
- Buttons: `Get Started →` (primary) · `View on GitHub` (outline)

### 6.3 Why SqlProof
- Label: `// why sqlproof`
- Heading: "Your queries work on your fixtures. Do they work on real data?"
- Lead text explaining the problem with hand-crafted fixtures
- 2×2 grid of feature cards:
  1. **Schema-aware generation** — respects FKs, CHECK, UNIQUE, NOT NULL, enums
  2. **Minimal counterexamples** — fast-check shrinks failures to the smallest dataset
  3. **Zero infrastructure** — disposable Postgres via testcontainers, isolated schemas per run
  4. **Works with your test runner** — drop into Vitest or Jest

### 6.4 How It Works
- Label: `// how it works`
- Heading: "Four steps, zero boilerplate"
- 4-step horizontal row:
  1. Parse schema
  2. Generate data
  3. Insert & run
  4. Report

### 6.5 Code Example
- Label: `// example`
- Heading: "See it in action"
- Code block showing `SqlProof.connect()` + `proof.invariant()` + failure output comment
- File tab: `orders.test.ts`

### 6.6 Footer
- Left: `SqlProof` logo + "MIT License · Built on fast-check, pg, testcontainers"
- Right: Docs · GitHub · npm links

---

## 7. Docs Section

Powered by Starlight. Sidebar navigation:

```
Getting Started
API Reference
  └── SqlProof Class
  └── CheckOptions
  └── TableCustomization
Guides
  └── FK Distribution Strategies
  └── Custom Generators
Examples
  └── E-Commerce Orders
```

Content is sourced and adapted from the existing `README.md` and `SPEC.md`. The docs use the same dark/green color scheme via Starlight's custom CSS variables.

---

## 8. Deployment

### GitHub Actions workflow (`.github/workflows/deploy-website.yml`):

```yaml
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
        working-directory: website
      - run: npm run build
        working-directory: website
      - uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: website/dist
```

GitHub Pages must be configured to serve from the `gh-pages` branch.

---

## 9. README Update

After the website is live, update the repo `README.md` to add a link to the website at the top (e.g., "→ Full docs at sqlproof.dev").

---

## 10. Non-Goals

- Custom domain setup — out of scope (GitHub Pages default URL is fine for v1)
- Blog or changelog — not needed for v1
- Dark/light mode toggle — dark only
- Internationalization — English only
- Search backend — Starlight's built-in pagefind (static search) is sufficient
