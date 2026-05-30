# Security Policy

## Supported versions

sqlproof is in pre-1.0. Only the latest `0.x` release receives security
fixes. We don't backport patches to older `0.x` versions — if a
vulnerability is discovered, upgrading to the latest minor is the
supported remediation path.

| Version  | Supported          |
| -------- | ------------------ |
| latest 0.x | :white_check_mark: |
| older 0.x  | :x:                |

Once sqlproof reaches 1.0, this table will be updated to reflect a
maintenance policy for previous major versions.

## Reporting a vulnerability

**Please do not report security vulnerabilities via public GitHub issues
or pull requests.**

Use GitHub's private security advisories instead:

1. Go to <https://github.com/alialavia/sqlproof/security/advisories/new>.
2. Fill in the details. The maintainer is automatically notified.

This gives us a private space to discuss the issue, prepare a fix, and
coordinate disclosure before anything becomes public.

If for any reason you can't use GitHub Security Advisories, email
**al@generativemodels.ai** with the subject line `sqlproof security`.

## Expected response time

The maintainer aims to acknowledge security reports within **5 business
days**. A coordinated disclosure timeline is agreed during follow-up,
typically targeting a fix release within 30 days for high-severity issues.

## Scope

In-scope:

- Anything in `src/sqlproof/` that could let an attacker execute SQL
  outside the test schema, read environment variables, or escape the
  testcontainer/process boundary.
- The data generator producing values that, if used in production,
  could expose the user to second-order injection or data exfiltration.

Out of scope:

- Issues that require write access to `pyproject.toml` or
  `release-please-config.json` (we trust maintainers).
- Vulnerabilities in upstream dependencies (`hypothesis`, `psycopg`,
  `pglast`) — report those to the respective projects.
- Test failures, false-positive property failures, or generator
  inefficiency — those are bugs; please use the regular bug report
  template.

Thanks for helping keep sqlproof and its users safe.
