<!--
Thanks for the contribution! A few quick notes before you fill this in:

1. The PR title must follow Conventional Commits (e.g. `feat(generator):`,
   `fix(core):`, `docs(readme):`). CI will fail if it doesn't parse.
   See CONTRIBUTING.md for the full list of accepted types.

2. The PR title becomes the squash-merge commit message on main and is what
   release-please uses to compute the next version + changelog entry. Make
   it accurate and self-contained.

3. Delete sections below that don't apply.
-->

## Summary

<!-- What changed? Bullets are great. -->

-

## Why

<!-- The motivation. What problem does this solve, or what user request does
it satisfy? Link to the issue if there is one. -->

## Test plan

<!-- Checklist of what you ran/verified locally. Tick the boxes you've
completed; leave unticked the things you'd like a reviewer to verify. -->

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check src/ tests/` clean
- [ ] `uv run pyright` clean
- [ ] `uv run mypy src/sqlproof/` clean
- [ ] (if integration-affecting) tested locally against `supabase/postgres:15.8.1.040`
- [ ] (if user-facing) updated docs / README / examples
- [ ] (if breaking) PR title includes `!` or body includes `BREAKING CHANGE:` footer

## Related

<!-- Issues this closes, PRs this depends on, follow-ups filed. -->

- Closes #
