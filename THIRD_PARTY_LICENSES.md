# Third-party licenses

SqlProof itself is MIT licensed. It does not vendor or bundle any
third-party code: dependencies are declared in `pyproject.toml` and
installed separately by pip or uv, each under its own license.

This file lists the runtime dependencies that are **not** MIT licensed
and what that means for you. Everything else in `uv.lock` and
`website/package-lock.json` uses a permissive license (MIT, BSD, Apache-2.0,
ISC, PSF and similar).

## Runtime dependencies (`pip install sqlproof`)

| Package | License | What it means |
|---|---|---|
| [postgast](https://github.com/eddieland/postgast) | BSD-2-Clause | Bundles libpg_query (BSD-3-Clause), PostgreSQL parser code (PostgreSQL License), protobuf-c (BSD-2-Clause), and xxHash (BSD-2-Clause). Preserve their notices when redistributing. |
| [PyGreSQL](https://github.com/PyGreSQL/PyGreSQL) | PostgreSQL | Permissive. Preserve its copyright and license notices when redistributing. It links to the separately installed libpq client library (also PostgreSQL licensed); no Python runtime dependencies. |
| [protobuf](https://github.com/protocolbuffers/protobuf) | BSD-3-Clause | Permissive. Used to read postgast ASTs. |
| [hypothesis](https://github.com/HypothesisWorks/hypothesis) | MPL-2.0 | File-level copyleft: only changes to Hypothesis's own files must be shared under MPL-2.0. Using it unmodified has no obligations. |
| [sortedcontainers](https://github.com/grantjenks/python-sortedcontainers), tzdata | Apache-2.0 | Permissive. Keep the license and NOTICE file if you redistribute them. |
| [pygments](https://github.com/pygments/pygments) | BSD-2-Clause | Permissive. |
| typing-extensions | PSF-2.0 | Permissive. |

Optional extras (`testcontainers`, `pydantic`, `mcp`) pull in further
dependencies under permissive licenses, plus `certifi` (MPL-2.0, unmodified).

## Native system dependencies

The Python dependencies and postgast's bundled parser components contain no
GPL or LGPL code. The separately installed PostgreSQL client stack depends on
the operating system and distribution; the lockfile license job does not
audit it.

For example, the Debian Linux `libpq` used in the installation smoke test
links LGPL system libraries: glibc and, through Kerberos, `libkeyutils`.
[Keyutils's library is LGPL licensed](https://sources.debian.org/src/keyutils/1.6.3-2/README/).
Homebrew's `libpq` formula also installs GPL readline for its command-line
tools; the macOS PyGreSQL extension and `libpq` inspected here do not link
readline. SqlProof's wheel bundles none of these system libraries.

Removing pglast and psycopg removes the GPL/LGPL Python packages from the
runtime dependency graph. It does not establish that every native component
on a deployment host is free of GPL/LGPL. Audit the chosen client-library
distribution separately if that is required by your policy.

## Development and website dependencies

These are not installed with SqlProof and are never distributed to users.
The only copyleft ones are:

- `pathspec` (MPL-2.0), via mypy.
- `@img/sharp-libvips-*` (LGPL-3.0-or-later), the image library sharp uses
  while building the website. It runs at build time only; nothing from it
  ships in the published site.

## How this is enforced

The `licenses` job in `.github/workflows/ci.yml` runs
[OSV-Scanner](https://github.com/google/osv-scanner) over `uv.lock` and
`website/package-lock.json` on every pull request. It fails when a
dependency uses a license outside the allowlist: permissive licenses plus
MPL-2.0 and LGPL-3.0. GPL and AGPL are not allowed.

Exceptions and corrections live in `.github/osv-scanner-licenses.toml`:

- **colorama, libcst, nodeenv, pywin32** publish no standard license id in
  their package metadata, so their licenses are set by hand after reading
  each package's LICENSE file. The overrides are pinned to a version, so an
  upgrade fails the check until the new version is re-checked.
- **PyGreSQL** uses the older license name in its metadata; its shipped
  LICENSE.txt grants the PostgreSQL License. This correction is also pinned.

To check locally, install [osv-scanner](https://google.github.io/osv-scanner/installation/)
and run the same arguments as the CI job:

```bash
osv-scanner scan source \
  --config=.github/osv-scanner-licenses.toml \
  --licenses=MIT,0BSD,ISC,BSD-2-Clause,BSD-3-Clause,PostgreSQL,Apache-2.0,PSF-2.0,Python-2.0,BlueOak-1.0.0,CC0-1.0,MPL-2.0,LGPL-3.0-only,LGPL-3.0-or-later \
  --lockfile=uv.lock --lockfile=website/package-lock.json
```

If the check fails on a new dependency, prefer a permissively licensed
alternative. If there is none, discuss it in the pull request before adding
an exception.
