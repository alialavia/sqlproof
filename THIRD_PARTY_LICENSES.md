# Third-party licenses

SqlProof itself is MIT licensed (see [LICENSE](LICENSE)). It does not
vendor or bundle any third-party code: dependencies are declared in
`pyproject.toml` and installed separately by pip or uv, each under its own
license.

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

## System libraries

PyGreSQL connects to PostgreSQL through libpq, the PostgreSQL C client
library, which you install separately (from your OS package manager,
Homebrew, or a PostgreSQL installer). SqlProof's packages do not bundle it.

libpq itself uses the PostgreSQL License, but the libraries it links depend
on how your distribution built it. Debian's `libpq`, for example, links LGPL
system libraries: glibc and, through Kerberos,
[`libkeyutils`](https://sources.debian.org/src/keyutils/1.6.3-2/README/).
Homebrew's `libpq` formula also installs GNU readline (GPL), which is used
only by command-line tools such as `psql`, not by the client library.

The license check described below covers Python and npm packages only, not
system libraries. If your policy also covers those, check the libpq build
you install.

## Development and website dependencies

These are not installed with SqlProof and are never distributed to users.
The only copyleft ones are:

- `pathspec` (MPL-2.0), via mypy.
- `@img/sharp-libvips-*`, `@img/sharp-wasm32` and `@img/sharp-win32-*`
  (LGPL-3.0-or-later), the libvips binaries sharp uses to resize images
  while building the website. They run at build time only; nothing from
  them ships in the published site.

## How this is enforced

The `licenses` job in `.github/workflows/ci.yml` runs
[OSV-Scanner](https://github.com/google/osv-scanner) over `uv.lock` and
`website/package-lock.json` on every pull request. It fails when a
dependency uses a license outside the allowlist: permissive licenses plus
MPL-2.0. GPL, LGPL and AGPL are not allowed.

Exceptions and corrections live in `.github/osv-scanner-licenses.toml`:

- **colorama, libcst, nodeenv, pywin32** publish no standard license id in
  their package metadata, so their licenses are set by hand after reading
  each package's LICENSE file. The overrides are pinned to a version, so an
  upgrade fails the check until the new version is re-checked.
- **PyGreSQL** uses the older license name in its metadata; its shipped
  LICENSE.txt grants the PostgreSQL License. This correction is also pinned.
- **sharp's libvips packages** (website only) are LGPL and are exempted by
  name, because they are build tools that are never distributed. Runtime
  dependencies in `uv.lock` get no such exemption.
- **sqlproof** itself is set to MIT, because release PRs list a version
  that is not on PyPI yet.

To check locally, install [osv-scanner](https://google.github.io/osv-scanner/installation/)
and run the same arguments as the CI job:

```bash
osv-scanner scan source \
  --config=.github/osv-scanner-licenses.toml \
  --licenses=MIT,0BSD,ISC,BSD-2-Clause,BSD-3-Clause,PostgreSQL,Apache-2.0,PSF-2.0,Python-2.0,BlueOak-1.0.0,CC0-1.0,MPL-2.0 \
  --lockfile=uv.lock --lockfile=website/package-lock.json
```

If the check fails on a new dependency, prefer a permissively licensed
alternative. If there is none, discuss it in the pull request before adding
an exception.
