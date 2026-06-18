# Mutation Run Persistence + Local Report/Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each mutation run as a JSON artifact and add a `sqlproof mutation report` command that renders a self-contained, trend-first HTML dashboard from those artifacts.

**Architecture:** Three units behind clean seams. (1) Persistence: a `RunArtifact` dataclass + serialization (`mutation/artifact.py`) and a `save_run` writer (`mutation/persist.py`); `run_mutation_tests` gains an `artifact_dir=` param. (2) Aggregation: pure functions (`mutation/report/aggregate.py`) that read a directory of artifacts and emit a plain view model — no DB, no HTML. (3) Rendering + CLI: an HTML renderer (`mutation/report/render.py`) and a `mutation report` subcommand wired into `cli.py`. The aggregation layer's only input is the artifact directory, so the future cloud tier can reuse the scoring/trend/triage logic over ingested runs.

**Tech Stack:** Python 3.12, stdlib only for new runtime code (`dataclasses`, `json`, `pathlib`, `subprocess`, `datetime`, `secrets`, `html`, inline SVG strings). pytest for tests. Reuses `sqlproof.schema.fingerprint.compute` and `sqlproof.schema.parse_sql.parse_schema_sql`.

**Branch:** Work happens on `docs/mutation-dashboard-design-draft` (already checked out; the approved spec lives there).

---

## File Structure

- Create `src/sqlproof/mutation/artifact.py` — `SCHEMA_VERSION`, the `RunArtifact` dataclass, and `to_json_dict` / `from_json_dict`. The shared on-disk contract used by both the writer and the reader.
- Create `src/sqlproof/mutation/persist.py` — `save_run`, run-id generation, and best-effort git metadata capture.
- Modify `src/sqlproof/mutation/result.py` — add `duration_s` to `MutantOutcome` and `outcome_for_exit_code`.
- Modify `src/sqlproof/mutation/runner.py` — time each mutant, thread `duration_s` through; add `artifact_dir=` to `run_mutation_tests` and call `save_run`.
- Create `src/sqlproof/mutation/report/__init__.py` — package marker + public exports.
- Create `src/sqlproof/mutation/report/aggregate.py` — `load_runs` and `build_report` (pure view-model logic).
- Create `src/sqlproof/mutation/report/render.py` — `render_html(report) -> str`.
- Modify `src/sqlproof/cli.py` — add the `mutation report` subcommand group.
- Modify `src/sqlproof/mutation/__init__.py` — export `save_run`, `RunArtifact`.
- Modify `.gitignore` — add a `.sqlproof/` note.
- Tests: `tests/unit/test_mutation_artifact.py`, `tests/unit/test_mutation_persist.py`, `tests/unit/test_mutation_report_aggregate.py`, `tests/unit/test_mutation_report_render.py`, `tests/unit/test_mutation_id_stability.py`; extend `tests/unit/test_mutation_runner.py` and `tests/unit/test_cli_smoke.py`.

---

## Task 1: Add `duration_s` to `MutantOutcome`

**Files:**
- Modify: `src/sqlproof/mutation/result.py`
- Test: `tests/unit/test_mutation_result.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_mutation_result.py`:

```python
def test_outcome_carries_duration_when_provided() -> None:
    outcome = outcome_for_exit_code(
        mutant_id="abc",
        target="f",
        description="f: drop 'x'",
        expect_survives=False,
        exit_code=1,
        hypothesis_seed=7,
        detail=None,
        duration_s=3.5,
    )
    assert outcome.duration_s == 3.5


def test_outcome_duration_defaults_to_none() -> None:
    outcome = outcome_for_exit_code(
        mutant_id="abc",
        target="f",
        description="f: drop 'x'",
        expect_survives=False,
        exit_code=1,
        hypothesis_seed=7,
        detail=None,
    )
    assert outcome.duration_s is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_result.py::test_outcome_carries_duration_when_provided -v`
Expected: FAIL with `TypeError: outcome_for_exit_code() got an unexpected keyword argument 'duration_s'`.

- [ ] **Step 3: Add the field and parameter**

In `src/sqlproof/mutation/result.py`, add `duration_s` as the last field of `MutantOutcome` (after `detail`), defaulted so existing positional/keyword constructions keep working:

```python
@dataclass(frozen=True, slots=True)
class MutantOutcome:
    mutant_id: str
    target: str
    description: str
    status: Status
    pytest_exit_code: int | None
    hypothesis_seed: int | None
    detail: str | None
    duration_s: float | None = None
```

Add the parameter to `outcome_for_exit_code` (also defaulted) and pass it through:

```python
def outcome_for_exit_code(
    *,
    mutant_id: str,
    target: str,
    description: str,
    expect_survives: bool,
    exit_code: int,
    hypothesis_seed: int | None,
    detail: str | None,
    duration_s: float | None = None,
) -> MutantOutcome:
    """pytest exit codes: 0 all passed, 1 tests failed, 2 interrupted,
    3 internal error, 4 usage error, 5 no tests collected. Only 0 and 1
    are evidence about the mutant; everything else is an error."""
    if exit_code == 1:
        status: Status = "unexpected_kill" if expect_survives else "killed"
    elif exit_code == 0:
        status = "expected_survivor" if expect_survives else "survived"
    else:
        status = "error"
    return MutantOutcome(
        mutant_id=mutant_id,
        target=target,
        description=description,
        status=status,
        pytest_exit_code=exit_code,
        hypothesis_seed=hypothesis_seed,
        detail=detail,
        duration_s=duration_s,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_result.py -v`
Expected: PASS (new tests pass; all existing tests still pass because `duration_s` defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/mutation/result.py tests/unit/test_mutation_result.py
git commit -m "feat(mutation): add duration_s to MutantOutcome"
```

---

## Task 2: Time each mutant in the runner

**Files:**
- Modify: `src/sqlproof/mutation/runner.py:87-117` (`_run_one`)
- Test: `tests/unit/test_mutation_runner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_mutation_runner.py`:

```python
def test_outcome_records_duration() -> None:
    runner = FakeRunner({"m1": 1})
    result = runner.run([_prepared("m1")])
    duration = result.outcomes[0].duration_s
    assert duration is not None
    assert duration >= 0.0


def test_error_outcome_also_records_duration() -> None:
    class ExplodingRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            raise OSError("boom")

    runner = ExplodingRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    assert result.outcomes[0].status == "error"
    assert result.outcomes[0].duration_s is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_runner.py::test_outcome_records_duration -v`
Expected: FAIL with `AssertionError` (`duration_s` is `None`).

- [ ] **Step 3: Add timing to `_run_one`**

In `src/sqlproof/mutation/runner.py`, add `import time` near the other stdlib imports (alphabetical: after `import threading`). Replace the body of `_run_one` so it captures a monotonic start time and threads the elapsed duration into both return paths:

```python
    def _run_one(self, prepared: PreparedMutant) -> MutantOutcome:
        clone_name = f"sqlproof_mutant_{prepared.mutant_id}"
        start = time.monotonic()
        try:
            clone_dsn = self._create_clone(clone_name)
            try:
                self._apply_ddl(clone_dsn, prepared.ddl)
                exit_code, output = self._run_pytest(clone_dsn)
            finally:
                self._drop_clone(clone_name)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if exc.__context__ is not None:
                detail += f" (during handling of: {exc.__context__!r})"
            return MutantOutcome(
                mutant_id=prepared.mutant_id,
                target=prepared.mutant.target_name,
                description=prepared.mutant.describe(),
                status="error",
                pytest_exit_code=None,
                hypothesis_seed=self.hypothesis_seed,
                detail=detail,
                duration_s=time.monotonic() - start,
            )
        return outcome_for_exit_code(
            mutant_id=prepared.mutant_id,
            target=prepared.mutant.target_name,
            description=prepared.mutant.describe(),
            expect_survives=prepared.mutant.expect_survives,
            exit_code=exit_code,
            hypothesis_seed=self.hypothesis_seed,
            detail=None if exit_code == 1 else output[-_OUTPUT_TAIL:],
            duration_s=time.monotonic() - start,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_runner.py -v`
Expected: PASS (new duration tests pass; all existing runner tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/mutation/runner.py tests/unit/test_mutation_runner.py
git commit -m "feat(mutation): record per-mutant duration in runner"
```

---

## Task 3: Run artifact schema + serialization

**Files:**
- Create: `src/sqlproof/mutation/artifact.py`
- Test: `tests/unit/test_mutation_artifact.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mutation_artifact.py`:

```python
from __future__ import annotations

from sqlproof.mutation.artifact import SCHEMA_VERSION, RunArtifact
from sqlproof.mutation.result import MutantOutcome


def _artifact() -> RunArtifact:
    return RunArtifact(
        run_id="a1b2c3d4",
        started_at="2026-06-11T14:32:05Z",
        duration_s=412.7,
        sqlproof_version="0.9.0",
        git_sha="58d0e84",
        git_dirty=False,
        hypothesis_seed=1234567890,
        schema_fingerprint="sha256:abc",
        pytest_args=("-m", "rls", "tests/"),
        outcomes=(
            MutantOutcome(
                mutant_id="m1",
                target="billing.compute_invoice",
                description="COALESCE(SUM(usage), 0) -> 1",
                status="killed",
                pytest_exit_code=1,
                hypothesis_seed=1234567890,
                detail=None,
                duration_s=8.3,
            ),
        ),
    )


def test_round_trips_through_json_dict() -> None:
    original = _artifact()
    restored = RunArtifact.from_json_dict(original.to_json_dict())
    assert restored == original


def test_to_json_dict_stamps_schema_version() -> None:
    payload = _artifact().to_json_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["outcomes"][0]["duration_s"] == 8.3


def test_from_json_dict_rejects_unknown_schema_version() -> None:
    import pytest

    from sqlproof.mutation.artifact import UnsupportedSchemaVersion

    payload = _artifact().to_json_dict()
    payload["schema_version"] = 999
    with pytest.raises(UnsupportedSchemaVersion):
        RunArtifact.from_json_dict(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_artifact.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sqlproof.mutation.artifact'`.

- [ ] **Step 3: Write the artifact module**

Create `src/sqlproof/mutation/artifact.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlproof.mutation.result import MutantOutcome, Status

SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(Exception):
    """Raised when an artifact declares a schema_version this build cannot read."""


@dataclass(frozen=True, slots=True)
class RunArtifact:
    run_id: str
    started_at: str  # ISO-8601 UTC, e.g. "2026-06-11T14:32:05Z"
    duration_s: float
    sqlproof_version: str
    git_sha: str | None
    git_dirty: bool
    hypothesis_seed: int | None
    schema_fingerprint: str | None
    pytest_args: tuple[str, ...]
    outcomes: tuple[MutantOutcome, ...]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "sqlproof_version": self.sqlproof_version,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "hypothesis_seed": self.hypothesis_seed,
            "schema_fingerprint": self.schema_fingerprint,
            "pytest_args": list(self.pytest_args),
            "outcomes": [_outcome_to_dict(o) for o in self.outcomes],
        }

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> RunArtifact:
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            msg = f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})"
            raise UnsupportedSchemaVersion(msg)
        return cls(
            run_id=str(payload["run_id"]),
            started_at=str(payload["started_at"]),
            duration_s=float(payload["duration_s"]),
            sqlproof_version=str(payload["sqlproof_version"]),
            git_sha=payload["git_sha"],
            git_dirty=bool(payload["git_dirty"]),
            hypothesis_seed=payload["hypothesis_seed"],
            schema_fingerprint=payload["schema_fingerprint"],
            pytest_args=tuple(payload["pytest_args"]),
            outcomes=tuple(_outcome_from_dict(o) for o in payload["outcomes"]),
        )


def _outcome_to_dict(outcome: MutantOutcome) -> dict[str, Any]:
    return {
        "mutant_id": outcome.mutant_id,
        "target": outcome.target,
        "description": outcome.description,
        "status": outcome.status,
        "pytest_exit_code": outcome.pytest_exit_code,
        "hypothesis_seed": outcome.hypothesis_seed,
        "detail": outcome.detail,
        "duration_s": outcome.duration_s,
    }


def _outcome_from_dict(payload: dict[str, Any]) -> MutantOutcome:
    return MutantOutcome(
        mutant_id=str(payload["mutant_id"]),
        target=str(payload["target"]),
        description=str(payload["description"]),
        status=cast(Status, payload["status"]),
        pytest_exit_code=payload["pytest_exit_code"],
        hypothesis_seed=payload["hypothesis_seed"],
        detail=payload["detail"],
        duration_s=payload.get("duration_s"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_artifact.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/mutation/artifact.py tests/unit/test_mutation_artifact.py
git commit -m "feat(mutation): add run artifact schema and serialization"
```

---

## Task 4: `save_run` writer + metadata helpers

**Files:**
- Create: `src/sqlproof/mutation/persist.py`
- Test: `tests/unit/test_mutation_persist.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mutation_persist.py`:

```python
from __future__ import annotations

import json

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.persist import new_run_id, save_run
from sqlproof.mutation.result import MutantOutcome


def _artifact(run_id: str = "a1b2c3d4") -> RunArtifact:
    return RunArtifact(
        run_id=run_id,
        started_at="2026-06-11T14:32:05Z",
        duration_s=1.0,
        sqlproof_version="0.9.0",
        git_sha=None,
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:abc",
        pytest_args=("tests/",),
        outcomes=(
            MutantOutcome(
                mutant_id="m1",
                target="f",
                description="f: drop 'x'",
                status="killed",
                pytest_exit_code=1,
                hypothesis_seed=42,
                detail=None,
                duration_s=0.5,
            ),
        ),
    )


def test_save_run_writes_readable_artifact(tmp_path) -> None:
    path = save_run(_artifact(), artifact_dir=tmp_path)
    assert path.exists()
    assert path.parent == tmp_path
    restored = RunArtifact.from_json_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored == _artifact()


def test_save_run_filename_sorts_chronologically(tmp_path) -> None:
    save_run(_artifact(run_id="aaaaaaaa"), artifact_dir=tmp_path)
    path = save_run(_artifact(run_id="bbbbbbbb"), artifact_dir=tmp_path)
    # started_at leads the filename so lexical sort == chronological sort
    assert path.name.startswith("2026-06-11T14-32-05Z-")


def test_save_run_creates_missing_directory(tmp_path) -> None:
    target = tmp_path / "nested" / "runs"
    save_run(_artifact(), artifact_dir=target)
    assert list(target.glob("*.json"))


def test_new_run_id_is_hex_and_distinct() -> None:
    ids = {new_run_id() for _ in range(10)}
    assert len(ids) == 10
    assert all(len(i) >= 8 for i in ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_persist.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sqlproof.mutation.persist'`.

- [ ] **Step 3: Write the persist module**

Create `src/sqlproof/mutation/persist.py`:

```python
from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

from sqlproof.mutation.artifact import RunArtifact

# started_at is ISO-8601 with ':' which is illegal in filenames on some
# platforms; replace with '-' so the timestamp still sorts chronologically.
_FILENAME_SAFE = str.maketrans({":": "-"})


def new_run_id() -> str:
    """A short random hex id; uniqueness within a runs dir, not crypto."""
    return secrets.token_hex(8)


def save_run(artifact: RunArtifact, *, artifact_dir: Path) -> Path:
    """Write *artifact* as one JSON file under *artifact_dir* and return the path.

    The directory is created if missing. The filename is
    ``<started_at>-<run_id[:6]>.json`` with ':' replaced by '-', so a
    lexical sort of the directory equals chronological order. The directory
    is append-only: this never rewrites an existing run.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = artifact.started_at.translate(_FILENAME_SAFE)
    path = artifact_dir / f"{stamp}-{artifact.run_id[:6]}.json"
    path.write_text(
        json.dumps(artifact.to_json_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def capture_git_info() -> tuple[str | None, bool]:
    """Return ``(short_sha, dirty)`` best-effort; ``(None, False)`` if not a
    git repo or git is unavailable. Never raises."""
    sha = _git(["rev-parse", "--short", "HEAD"])
    if sha is None:
        return None, False
    status = _git(["status", "--porcelain"])
    return sha, bool(status)


def _git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_persist.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/mutation/persist.py tests/unit/test_mutation_persist.py
git commit -m "feat(mutation): add save_run artifact writer and git metadata"
```

---

## Task 5: Wire `artifact_dir` into `run_mutation_tests`

**Files:**
- Modify: `src/sqlproof/mutation/runner.py:188-251` (`run_mutation_tests`)
- Modify: `src/sqlproof/mutation/__init__.py`
- Test: `tests/unit/test_mutation_runner.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_mutation_runner.py` (imports at top of file: add `from pathlib import Path` and `from sqlproof.mutation.artifact import RunArtifact`):

```python
def test_run_mutation_tests_writes_artifact_when_dir_given(tmp_path, monkeypatch) -> None:
    import json

    from sqlproof.mutation import runner as runner_module
    from sqlproof.mutation.model import Mutant, MutationSet, Replace
    from sqlproof.mutation.result import MutantOutcome, MutationResult

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
        encoding="utf-8",
    )

    # Replace LocalMutationRunner.run so no database is touched.
    def fake_run(self, prepared):  # type: ignore[no-untyped-def]
        return MutationResult(
            outcomes=tuple(
                MutantOutcome(
                    mutant_id=p.mutant_id,
                    target=p.mutant.target_name,
                    description=p.mutant.describe(),
                    status="killed",
                    pytest_exit_code=1,
                    hypothesis_seed=self.hypothesis_seed,
                    detail=None,
                    duration_s=0.1,
                )
                for p in prepared
            )
        )

    monkeypatch.setattr(runner_module.LocalMutationRunner, "run", fake_run)

    mutations = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),)
    )
    runs_dir = tmp_path / "runs"
    result = runner_module.run_mutation_tests(
        mutations,
        schema_file=schema_file,
        database_url="postgresql://localhost/base",
        pytest_args=["tests/"],
        hypothesis_seed=42,
        artifact_dir=runs_dir,
    )
    assert result.outcomes[0].status == "killed"
    files = list(runs_dir.glob("*.json"))
    assert len(files) == 1
    artifact = RunArtifact.from_json_dict(json.loads(files[0].read_text(encoding="utf-8")))
    assert artifact.hypothesis_seed == 42
    assert artifact.pytest_args == ("tests/",)
    assert artifact.schema_fingerprint is not None
    assert artifact.outcomes[0].mutant_id == result.outcomes[0].mutant_id


def test_run_mutation_tests_skips_artifact_when_no_dir(tmp_path, monkeypatch) -> None:
    from sqlproof.mutation import runner as runner_module
    from sqlproof.mutation.model import Mutant, MutationSet, Replace
    from sqlproof.mutation.result import MutationResult

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner_module.LocalMutationRunner,
        "run",
        lambda self, prepared: MutationResult(outcomes=()),
    )
    mutations = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),)
    )
    # No artifact_dir → must not create any files anywhere under tmp_path.
    runner_module.run_mutation_tests(
        mutations,
        schema_file=schema_file,
        database_url="postgresql://localhost/base",
        pytest_args=["tests/"],
    )
    assert not list(tmp_path.glob("**/*.json"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_runner.py::test_run_mutation_tests_writes_artifact_when_dir_given -v`
Expected: FAIL with `TypeError: run_mutation_tests() got an unexpected keyword argument 'artifact_dir'`.

- [ ] **Step 3: Add `artifact_dir` and persistence**

In `src/sqlproof/mutation/runner.py`, add these imports near the top (after the existing `from sqlproof.mutation...` imports):

```python
from datetime import datetime, timezone

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.persist import capture_git_info, new_run_id, save_run
from sqlproof.schema.fingerprint import compute as compute_fingerprint
from sqlproof.schema.parse_sql import parse_schema_sql
```

Add the `_version` import alongside the others:

```python
from sqlproof._version import __version__
```

Change the `run_mutation_tests` signature to accept `artifact_dir` (insert before `timeout_s`):

```python
def run_mutation_tests(
    mutations: MutationSet,
    *,
    schema_file: str | Path,
    database_url: str,
    pytest_args: Sequence[str],
    env_var: str = "SQLPROOF_TEST_DATABASE_URL",
    maintenance_db: str = "postgres",
    hypothesis_seed: int | None = None,
    max_workers: int = 1,
    artifact_dir: str | Path | None = None,
    timeout_s: float | None = 600.0,
) -> MutationResult:
```

Then replace the body from `schema_sql = ...` to the end with a timed run that persists when `artifact_dir` is set:

```python
    schema_sql = Path(schema_file).read_text(encoding="utf-8")
    prepared = prepare_mutants(mutations, schema_sql)
    hypothesis_seed = _resolve_seed(hypothesis_seed)
    runner = LocalMutationRunner(
        database_url=database_url,
        pytest_args=pytest_args,
        env_var=env_var,
        maintenance_db=maintenance_db,
        hypothesis_seed=hypothesis_seed,
        max_workers=max_workers,
        timeout_s=timeout_s,
    )
    started = datetime.now(timezone.utc)
    monotonic_start = time.monotonic()
    result = runner.run(prepared)
    if artifact_dir is not None:
        git_sha, git_dirty = capture_git_info()
        try:
            fingerprint: str | None = compute_fingerprint(parse_schema_sql(schema_sql))
        except Exception:
            fingerprint = None
        artifact = RunArtifact(
            run_id=new_run_id(),
            started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            duration_s=time.monotonic() - monotonic_start,
            sqlproof_version=__version__,
            git_sha=git_sha,
            git_dirty=git_dirty,
            hypothesis_seed=hypothesis_seed,
            schema_fingerprint=fingerprint,
            pytest_args=tuple(pytest_args),
            outcomes=result.outcomes,
        )
        save_run(artifact, artifact_dir=Path(artifact_dir))
    return result
```

Update the docstring of `run_mutation_tests` by adding this paragraph after the `max_workers` paragraph:

```
    `artifact_dir`, when given, persists this run as a JSON artifact under
    that directory (created if missing) via :func:`save_run`, capturing the
    resolved seed, schema fingerprint, git sha/dirty flag, and per-mutant
    outcomes. When ``None`` (the default) nothing is written. Point
    ``sqlproof mutation report`` at the same directory to visualize history.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_runner.py -v`
Expected: PASS (both new tests pass; all existing runner tests still pass).

- [ ] **Step 5: Export from the package**

In `src/sqlproof/mutation/__init__.py`, add the imports and `__all__` entries:

```python
from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.persist import save_run
```

Add `"RunArtifact"` and `"save_run"` to `__all__` (keep it sorted).

- [ ] **Step 6: Run the full mutation test module + import check**

Run: `uv run pytest tests/unit/test_mutation_runner.py -v && uv run python -c "from sqlproof.mutation import save_run, RunArtifact"`
Expected: PASS and no import error.

- [ ] **Step 7: Commit**

```bash
git add src/sqlproof/mutation/runner.py src/sqlproof/mutation/__init__.py tests/unit/test_mutation_runner.py
git commit -m "feat(mutation): persist run artifact from run_mutation_tests"
```

---

## Task 6: Aggregation — `load_runs`

**Files:**
- Create: `src/sqlproof/mutation/report/__init__.py`
- Create: `src/sqlproof/mutation/report/aggregate.py`
- Test: `tests/unit/test_mutation_report_aggregate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mutation_report_aggregate.py`:

```python
from __future__ import annotations

import json

from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.report.aggregate import load_runs
from sqlproof.mutation.result import MutantOutcome


def _write(dir_path, run_id, started_at, outcomes=()):  # type: ignore[no-untyped-def]
    artifact = RunArtifact(
        run_id=run_id,
        started_at=started_at,
        duration_s=1.0,
        sqlproof_version="0.9.0",
        git_sha="abc1234",
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:s1",
        pytest_args=("tests/",),
        outcomes=tuple(outcomes),
    )
    path = dir_path / f"{started_at.replace(':', '-')}-{run_id[:6]}.json"
    path.write_text(json.dumps(artifact.to_json_dict()), encoding="utf-8")
    return path


def test_load_runs_returns_runs_sorted_by_started_at(tmp_path) -> None:
    _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z")
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z")
    loaded = load_runs(tmp_path)
    assert [r.started_at for r in loaded.runs] == [
        "2026-06-11T10:00:00Z",
        "2026-06-12T10:00:00Z",
    ]
    assert loaded.skipped == []


def test_load_runs_skips_corrupt_and_unknown_version(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    bad_version = tmp_path / "bad-version.json"
    payload = json.loads((next(tmp_path.glob("2026*.json"))).read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    bad_version.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_runs(tmp_path)
    assert len(loaded.runs) == 1
    skipped_names = {s.path.name for s in loaded.skipped}
    assert skipped_names == {"broken.json", "bad-version.json"}
    assert all(s.reason for s in loaded.skipped)


def test_load_runs_on_missing_dir_returns_empty(tmp_path) -> None:
    loaded = load_runs(tmp_path / "does-not-exist")
    assert loaded.runs == []
    assert loaded.skipped == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_report_aggregate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sqlproof.mutation.report'`.

- [ ] **Step 3: Create the package marker**

Create `src/sqlproof/mutation/report/__init__.py`:

```python
from __future__ import annotations

from sqlproof.mutation.report.aggregate import build_report, load_runs
from sqlproof.mutation.report.render import render_html

__all__ = ["build_report", "load_runs", "render_html"]
```

(This imports `render_html`, added in Task 8; until then, comment out that line and the `__all__` entry, or implement Task 8 before importing the package. To keep tasks runnable in order, write `__init__.py` now with only the `aggregate` import and add `render_html` in Task 8.)

For this task, write `src/sqlproof/mutation/report/__init__.py` as:

```python
from __future__ import annotations

from sqlproof.mutation.report.aggregate import build_report, load_runs

__all__ = ["build_report", "load_runs"]
```

- [ ] **Step 4: Write `load_runs`**

Create `src/sqlproof/mutation/report/aggregate.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlproof.mutation.artifact import RunArtifact, UnsupportedSchemaVersion


@dataclass(frozen=True, slots=True)
class SkippedFile:
    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class LoadResult:
    runs: list[RunArtifact]
    skipped: list[SkippedFile]


def load_runs(runs_dir: Path) -> LoadResult:
    """Read every ``*.json`` artifact in *runs_dir*, sorted chronologically by
    ``started_at``. Unparseable files and unknown schema versions are skipped
    (recorded in ``skipped``), never raised — one bad artifact must not kill
    the report. A missing directory yields an empty result."""
    runs: list[RunArtifact] = []
    skipped: list[SkippedFile] = []
    if not runs_dir.is_dir():
        return LoadResult(runs=[], skipped=[])
    for path in sorted(runs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            runs.append(RunArtifact.from_json_dict(payload))
        except (json.JSONDecodeError, UnsupportedSchemaVersion, KeyError, ValueError, TypeError) as exc:
            skipped.append(SkippedFile(path=path, reason=f"{type(exc).__name__}: {exc}"))
    runs.sort(key=lambda r: r.started_at)
    return LoadResult(runs=runs, skipped=skipped)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_report_aggregate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/sqlproof/mutation/report/__init__.py src/sqlproof/mutation/report/aggregate.py tests/unit/test_mutation_report_aggregate.py
git commit -m "feat(mutation): load and validate run artifacts for reporting"
```

---

## Task 7: Aggregation — `build_report` view model

**Files:**
- Modify: `src/sqlproof/mutation/report/aggregate.py`
- Test: `tests/unit/test_mutation_report_aggregate.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_mutation_report_aggregate.py` (add `from sqlproof.mutation.report.aggregate import build_report` to the imports):

```python
def _outcome(mutant_id, target, status):  # type: ignore[no-untyped-def]
    return MutantOutcome(
        mutant_id=mutant_id,
        target=target,
        description=f"{target}: mutate {mutant_id}",
        status=status,
        pytest_exit_code=1 if status in ("killed", "unexpected_kill") else 0,
        hypothesis_seed=42,
        detail=None,
        duration_s=0.5,
    )


def test_build_report_computes_score_excluding_errors_and_expected(tmp_path) -> None:
    # killed + unexpected_kill = numerator; killed + survived + unexpected_kill = denominator.
    # error and expected_survivor excluded from both.
    _write(
        tmp_path,
        "aaaaaaaa",
        "2026-06-11T10:00:00Z",
        outcomes=[
            _outcome("k1", "f", "killed"),
            _outcome("s1", "f", "survived"),
            _outcome("e1", "f", "error"),
            _outcome("x1", "f", "expected_survivor"),
        ],
    )
    report = build_report(load_runs(tmp_path))
    run = report.runs[0]
    assert run.killed == 1
    assert run.survived == 1
    assert run.errored == 1
    assert run.score == 0.5  # 1 / (1 + 1)


def test_build_report_score_is_none_when_denominator_zero(tmp_path) -> None:
    _write(
        tmp_path,
        "aaaaaaaa",
        "2026-06-11T10:00:00Z",
        outcomes=[_outcome("e1", "f", "error"), _outcome("x1", "f", "expected_survivor")],
    )
    report = build_report(load_runs(tmp_path))
    assert report.runs[0].score is None


def test_build_report_classifies_new_vs_known_survivors(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("s1", "f", "survived")])
    _write(
        tmp_path,
        "bbbbbbbb",
        "2026-06-12T10:00:00Z",
        outcomes=[_outcome("s1", "f", "survived"), _outcome("s2", "f", "survived")],
    )
    report = build_report(load_runs(tmp_path))
    by_id = {s.mutant_id: s for s in report.latest_survivors}
    assert by_id["s1"].is_new is False  # seen in the earlier run
    assert by_id["s2"].is_new is True   # first appearance


def test_build_report_repro_command_includes_args_and_seed(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("s1", "f", "survived")])
    report = build_report(load_runs(tmp_path))
    cmd = report.latest_survivors[0].repro_command
    assert "pytest tests/" in cmd
    assert "--hypothesis-seed=42" in cmd
    assert "s1" in cmd


def test_build_report_flags_schema_drift(tmp_path) -> None:
    p1 = _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    p2 = _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    # Mutate p2's fingerprint so it differs from p1.
    payload = json.loads(p2.read_text(encoding="utf-8"))
    payload["schema_fingerprint"] = "sha256:DIFFERENT"
    p2.write_text(json.dumps(payload), encoding="utf-8")
    report = build_report(load_runs(tmp_path))
    assert report.runs[0].schema_changed is False  # first run
    assert report.runs[1].schema_changed is True   # fingerprint changed


def test_build_report_per_target_history(tmp_path) -> None:
    _write(tmp_path, "aaaaaaaa", "2026-06-11T10:00:00Z", outcomes=[_outcome("k1", "f", "killed")])
    _write(tmp_path, "bbbbbbbb", "2026-06-12T10:00:00Z", outcomes=[_outcome("k1", "f", "survived")])
    report = build_report(load_runs(tmp_path))
    target = next(t for t in report.targets if t.target == "f")
    assert [point.score for point in target.history] == [1.0, 0.0]
    assert target.latest_score == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_report_aggregate.py::test_build_report_computes_score_excluding_errors_and_expected -v`
Expected: FAIL with `ImportError: cannot import name 'build_report'`.

- [ ] **Step 3: Implement `build_report` and the view-model dataclasses**

Append to `src/sqlproof/mutation/report/aggregate.py`:

```python
@dataclass(frozen=True, slots=True)
class TrendPoint:
    started_at: str
    score: float | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    started_at: str
    git_sha: str | None
    git_dirty: bool
    duration_s: float
    killed: int
    survived: int
    errored: int
    score: float | None
    schema_fingerprint: str | None
    schema_changed: bool


@dataclass(frozen=True, slots=True)
class TargetSummary:
    target: str
    latest_score: float | None
    mutant_count: int
    survivor_count: int
    history: list[TrendPoint]


@dataclass(frozen=True, slots=True)
class SurvivorEntry:
    mutant_id: str
    target: str
    description: str
    repro_command: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class ReportData:
    runs: list[RunSummary]
    targets: list[TargetSummary]
    latest_survivors: list[SurvivorEntry]
    skipped: list[SkippedFile]


_SURVIVED = "survived"
_NUMERATOR = frozenset({"killed", "unexpected_kill"})
_DENOMINATOR = frozenset({"killed", "unexpected_kill", "survived"})


def _score(outcomes) -> float | None:  # type: ignore[no-untyped-def]
    denominator = sum(1 for o in outcomes if o.status in _DENOMINATOR)
    if denominator == 0:
        return None
    numerator = sum(1 for o in outcomes if o.status in _NUMERATOR)
    return numerator / denominator


def _repro_command(pytest_args, seed, mutant_id) -> str:  # type: ignore[no-untyped-def]
    args = " ".join(pytest_args)
    seed_flag = f" --hypothesis-seed={seed}" if seed is not None else ""
    return f"pytest {args}{seed_flag}  # mutant {mutant_id}"


def build_report(load_result: LoadResult) -> ReportData:
    """Turn loaded artifacts (chronological) into the dashboard view model:
    per-run scores, per-target history, latest-run survivors classified as
    new vs known, and schema-drift flags. Pure — no IO."""
    runs = load_result.runs
    run_summaries: list[RunSummary] = []
    prev_fingerprint: str | None = None
    for index, run in enumerate(runs):
        run_summaries.append(
            RunSummary(
                run_id=run.run_id,
                started_at=run.started_at,
                git_sha=run.git_sha,
                git_dirty=run.git_dirty,
                duration_s=run.duration_s,
                killed=sum(1 for o in run.outcomes if o.status in ("killed", "unexpected_kill")),
                survived=sum(1 for o in run.outcomes if o.status == _SURVIVED),
                errored=sum(1 for o in run.outcomes if o.status == "error"),
                score=_score(run.outcomes),
                schema_fingerprint=run.schema_fingerprint,
                schema_changed=index > 0 and run.schema_fingerprint != prev_fingerprint,
            )
        )
        prev_fingerprint = run.schema_fingerprint

    targets = _build_targets(runs)
    latest_survivors = _build_latest_survivors(runs)
    return ReportData(
        runs=run_summaries,
        targets=targets,
        latest_survivors=latest_survivors,
        skipped=load_result.skipped,
    )


def _build_targets(runs) -> list[TargetSummary]:  # type: ignore[no-untyped-def]
    names = sorted({o.target for run in runs for o in run.outcomes})
    summaries: list[TargetSummary] = []
    for name in names:
        history = [
            TrendPoint(
                started_at=run.started_at,
                score=_score([o for o in run.outcomes if o.target == name]),
            )
            for run in runs
            if any(o.target == name for o in run.outcomes)
        ]
        latest_run = next(
            (run for run in reversed(runs) if any(o.target == name for o in run.outcomes)),
            None,
        )
        latest_outcomes = (
            [o for o in latest_run.outcomes if o.target == name] if latest_run else []
        )
        summaries.append(
            TargetSummary(
                target=name,
                latest_score=_score(latest_outcomes),
                mutant_count=len(latest_outcomes),
                survivor_count=sum(1 for o in latest_outcomes if o.status == _SURVIVED),
                history=history,
            )
        )
    return summaries


def _build_latest_survivors(runs) -> list[SurvivorEntry]:  # type: ignore[no-untyped-def]
    if not runs:
        return []
    latest = runs[-1]
    earlier_survivor_ids = {
        o.mutant_id
        for run in runs[:-1]
        for o in run.outcomes
        if o.status == _SURVIVED
    }
    entries: list[SurvivorEntry] = []
    for outcome in latest.outcomes:
        if outcome.status != _SURVIVED:
            continue
        entries.append(
            SurvivorEntry(
                mutant_id=outcome.mutant_id,
                target=outcome.target,
                description=outcome.description,
                repro_command=_repro_command(
                    latest.pytest_args, outcome.hypothesis_seed, outcome.mutant_id
                ),
                is_new=outcome.mutant_id not in earlier_survivor_ids,
            )
        )
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_report_aggregate.py -v`
Expected: PASS (all build_report tests pass).

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/mutation/report/aggregate.py tests/unit/test_mutation_report_aggregate.py
git commit -m "feat(mutation): build dashboard view model from run artifacts"
```

---

## Task 8: HTML rendering

**Files:**
- Create: `src/sqlproof/mutation/report/render.py`
- Modify: `src/sqlproof/mutation/report/__init__.py`
- Test: `tests/unit/test_mutation_report_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mutation_report_render.py`:

```python
from __future__ import annotations

import re

from sqlproof.mutation.report.aggregate import (
    LoadResult,
    ReportData,
    RunSummary,
    SkippedFile,
    SurvivorEntry,
    TargetSummary,
    TrendPoint,
    build_report,
)
from sqlproof.mutation.report.render import render_html


def _report() -> ReportData:
    return ReportData(
        runs=[
            RunSummary(
                run_id="aaaaaaaa",
                started_at="2026-06-11T10:00:00Z",
                git_sha="abc1234",
                git_dirty=False,
                duration_s=12.0,
                killed=3,
                survived=1,
                errored=0,
                score=0.75,
                schema_fingerprint="sha256:s1",
                schema_changed=False,
            )
        ],
        targets=[
            TargetSummary(
                target="billing.f",
                latest_score=0.75,
                mutant_count=4,
                survivor_count=1,
                history=[TrendPoint(started_at="2026-06-11T10:00:00Z", score=0.75)],
            )
        ],
        latest_survivors=[
            SurvivorEntry(
                mutant_id="s1",
                target="billing.f",
                description="drop FILTER (WHERE active)",
                repro_command="pytest tests/ --hypothesis-seed=42  # mutant s1",
                is_new=True,
            )
        ],
        skipped=[],
    )


def test_render_returns_self_contained_html() -> None:
    html = render_html(_report())
    assert html.lstrip().lower().startswith("<!doctype html")
    # No external resources: no http(s) src/href references.
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)


def test_render_embeds_survivor_and_repro() -> None:
    html = render_html(_report())
    assert "billing.f" in html
    assert "pytest tests/ --hypothesis-seed=42" in html
    assert "NEW" in html  # new-survivor badge


def test_render_escapes_html_in_descriptions() -> None:
    report = _report()
    report.latest_survivors[0] = SurvivorEntry(
        mutant_id="s1",
        target="t",
        description="x < y AND <script>alert(1)</script>",
        repro_command="pytest",
        is_new=False,
    )
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_empty_report_says_no_runs() -> None:
    empty = build_report(LoadResult(runs=[], skipped=[]))
    html = render_html(empty)
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "no runs found" in html.lower()


def test_render_lists_skipped_files() -> None:
    report = _report()
    from pathlib import Path

    report.skipped.append(SkippedFile(path=Path("broken.json"), reason="JSONDecodeError: x"))
    html = render_html(report)
    assert "broken.json" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_report_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sqlproof.mutation.report.render'`.

- [ ] **Step 3: Implement the renderer**

Create `src/sqlproof/mutation/report/render.py`. The layout is trend-first: hero SVG score-over-time chart, per-target table, latest-run survivors, run log, then any skipped files. All CSS/JS is inline; the run data is embedded as a JSON blob; the chart is hand-built inline SVG. Every dynamic string is escaped with `html.escape`.

```python
from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from sqlproof.mutation.report.aggregate import ReportData

_CSS = """
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#7d8590; --line:#1a7f37;
          --bad:#cf222e; --new:#bf3989; --panel:#161b22; --border:#30363d; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  h1 { font-size:20px; } h2 { font-size:15px; margin:28px 0 8px; }
  .muted { color:var(--muted); }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; }
  .badge-new { background:var(--new); color:#fff; border-radius:10px;
               padding:1px 8px; font-size:11px; font-weight:700; }
  .panel { background:var(--panel); border:1px solid var(--border);
           border-radius:8px; padding:16px; }
  code { background:#0d1117; border:1px solid var(--border); border-radius:4px;
         padding:2px 6px; font-size:12px; }
  .drift { color:#d29922; }
  .empty { text-align:center; color:var(--muted); padding:60px 0; }
"""


def render_html(report: ReportData) -> str:
    if not report.runs:
        body = '<div class="empty">No runs found. Run mutations with '
        body += "<code>run_mutation_tests(..., artifact_dir=...)</code> first.</div>"
        return _page(body, report)
    sections = [
        _chart_section(report),
        _targets_section(report),
        _survivors_section(report),
        _runlog_section(report),
        _skipped_section(report),
    ]
    return _page("\n".join(sections), report)


def _page(body: str, report: ReportData) -> str:
    blob = json.dumps(
        {
            "runs": [asdict(r) for r in report.runs],
            "targets": [asdict(t) for t in report.targets],
        }
    )
    # Escape '</' inside the JSON so a "</script>" in data can't end the tag.
    blob = blob.replace("</", "<\\/")
    return (
        "<!doctype html>\n<html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>SqlProof Mutation Report</title>"
        f"<style>{_CSS}</style></head><body>"
        "<h1>SqlProof — Mutation Report</h1>"
        f"{body}"
        f"<script id=report-data type=application/json>{blob}</script>"
        "</body></html>"
    )


def _chart_section(report: ReportData) -> str:
    points = [(r.started_at, r.score) for r in report.runs if r.score is not None]
    if not points:
        return "<h2>Mutation score over time</h2><p class=muted>No scored runs yet.</p>"
    width, height, pad = 720, 220, 30
    n = len(points)
    def x(i: int) -> float:
        return pad if n == 1 else pad + i * (width - 2 * pad) / (n - 1)
    def y(score: float) -> float:
        return height - pad - score * (height - 2 * pad)
    coords = " ".join(f"{x(i):.1f},{y(s):.1f}" for i, (_, s) in enumerate(points))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(s):.1f}" r="3" fill="var(--line)"/>'
        for i, (_, s) in enumerate(points)
    )
    drift = "".join(
        f'<line x1="{x(i):.1f}" y1="{pad}" x2="{x(i):.1f}" y2="{height - pad}"'
        ' stroke="var(--drift,#d29922)" stroke-dasharray="3 3"/>'
        for i, r in enumerate(report.runs)
        if r.schema_changed and r.score is not None
    )
    latest = f"{points[-1][1] * 100:.0f}%"
    return (
        "<h2>Mutation score over time</h2>"
        f"<div class=panel><div style='font-size:32px;font-weight:700'>{latest}"
        "<span class=muted style='font-size:14px'> latest</span></div>"
        f"<svg viewBox='0 0 {width} {height}' width='100%'>"
        f"<polyline fill=none stroke='var(--line)' stroke-width=2 points='{coords}'/>"
        f"{dots}{drift}</svg>"
        "<p class=muted>Dashed gold lines mark runs where the schema fingerprint changed.</p>"
        "</div>"
    )


def _targets_section(report: ReportData) -> str:
    rows = []
    for t in report.targets:
        score = "—" if t.latest_score is None else f"{t.latest_score * 100:.0f}%"
        rows.append(
            f"<tr><td>{html.escape(t.target)}</td><td>{score}</td>"
            f"<td>{t.mutant_count}</td><td>{t.survivor_count}</td></tr>"
        )
    return (
        "<h2>Per-target (latest run)</h2><table>"
        "<tr><th>Target</th><th>Score</th><th>Mutants</th><th>Survivors</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _survivors_section(report: ReportData) -> str:
    if not report.latest_survivors:
        return "<h2>Survivors (latest run)</h2><p class=muted>None — all mutants killed.</p>"
    rows = []
    for s in report.latest_survivors:
        badge = "<span class=badge-new>NEW</span> " if s.is_new else ""
        rows.append(
            f"<tr><td>{badge}{html.escape(s.target)}</td>"
            f"<td>{html.escape(s.description)}</td>"
            f"<td><code>{html.escape(s.repro_command)}</code></td></tr>"
        )
    return (
        "<h2>Survivors (latest run)</h2><table>"
        "<tr><th>Target</th><th>Mutation</th><th>Reproduce</th></tr>"
        f"{''.join(rows)}</table>"
    )


def _runlog_section(report: ReportData) -> str:
    rows = []
    for r in report.runs:
        score = "—" if r.score is None else f"{r.score * 100:.0f}%"
        sha = html.escape(r.git_sha or "—") + ("*" if r.git_dirty else "")
        drift = " <span class=drift>(schema changed)</span>" if r.schema_changed else ""
        rows.append(
            f"<tr><td>{html.escape(r.started_at)}{drift}</td><td>{sha}</td>"
            f"<td>{score}</td><td>{r.duration_s:.1f}s</td></tr>"
        )
    return (
        "<h2>Run log</h2><table>"
        "<tr><th>Started</th><th>Commit</th><th>Score</th><th>Duration</th></tr>"
        f"{''.join(reversed(rows))}</table>"
    )


def _skipped_section(report: ReportData) -> str:
    if not report.skipped:
        return ""
    items = "".join(
        f"<li><code>{html.escape(s.path.name)}</code> — {html.escape(s.reason)}</li>"
        for s in report.skipped
    )
    return f"<h2>Skipped artifacts</h2><ul>{items}</ul>"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_report_render.py -v`
Expected: PASS.

- [ ] **Step 5: Add `render_html` to the package exports**

In `src/sqlproof/mutation/report/__init__.py`, add the render import and export:

```python
from __future__ import annotations

from sqlproof.mutation.report.aggregate import build_report, load_runs
from sqlproof.mutation.report.render import render_html

__all__ = ["build_report", "load_runs", "render_html"]
```

- [ ] **Step 6: Run the report test suite + import check**

Run: `uv run pytest tests/unit/test_mutation_report_aggregate.py tests/unit/test_mutation_report_render.py -v && uv run python -c "from sqlproof.mutation.report import render_html, build_report, load_runs"`
Expected: PASS and no import error.

- [ ] **Step 7: Commit**

```bash
git add src/sqlproof/mutation/report/render.py src/sqlproof/mutation/report/__init__.py tests/unit/test_mutation_report_render.py
git commit -m "feat(mutation): render self-contained HTML mutation report"
```

---

## Task 9: CLI `sqlproof mutation report`

**Files:**
- Modify: `src/sqlproof/cli.py`
- Test: `tests/unit/test_cli_smoke.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cli_smoke.py`:

```python
def test_mutation_report_on_empty_dir_writes_no_runs_page(tmp_path) -> None:
    output = tmp_path / "report.html"
    runs_dir = tmp_path / "runs"
    assert main(["mutation", "report", "--runs-dir", str(runs_dir), "--output", str(output)]) == 0
    html = output.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "no runs found" in html.lower()


def test_mutation_report_renders_existing_runs(tmp_path) -> None:
    import json

    from sqlproof.mutation.artifact import RunArtifact
    from sqlproof.mutation.result import MutantOutcome

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    artifact = RunArtifact(
        run_id="aaaaaaaa",
        started_at="2026-06-11T10:00:00Z",
        duration_s=5.0,
        sqlproof_version="0.9.0",
        git_sha="abc1234",
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:s1",
        pytest_args=("tests/",),
        outcomes=(
            MutantOutcome(
                mutant_id="s1",
                target="billing.f",
                description="drop FILTER",
                status="survived",
                pytest_exit_code=0,
                hypothesis_seed=42,
                detail=None,
                duration_s=0.5,
            ),
        ),
    )
    (runs_dir / "run.json").write_text(json.dumps(artifact.to_json_dict()), encoding="utf-8")

    output = tmp_path / "report.html"
    assert main(["mutation", "report", "--runs-dir", str(runs_dir), "--output", str(output)]) == 0
    html = output.read_text(encoding="utf-8")
    assert "billing.f" in html
    assert "--hypothesis-seed=42" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_smoke.py::test_mutation_report_on_empty_dir_writes_no_runs_page -v`
Expected: FAIL — argparse exits non-zero / raises `SystemExit` because `mutation` is not a known subcommand.

- [ ] **Step 3: Add the `mutation` subcommand group**

In `src/sqlproof/cli.py`, after the `clean-orphans` parser is registered (line ~45, before `args = parser.parse_args(argv)`), add a nested subparser group:

```python
    mutation = subcommands.add_parser("mutation")
    mutation_sub = mutation.add_subparsers(dest="mutation_command", required=True)
    mutation_report = mutation_sub.add_parser("report")
    mutation_report.add_argument(
        "--runs-dir", type=Path, default=Path(".sqlproof/mutation-runs")
    )
    mutation_report.add_argument("--output", type=Path, default=Path("mutation-report.html"))
    mutation_report.add_argument("--open", action="store_true", dest="open_browser")
```

Then add the dispatch branch alongside the other `if args.command == ...` blocks (before the final `return 1`):

```python
    if args.command == "mutation":
        if args.mutation_command == "report":
            return _mutation_report(args.runs_dir, args.output, open_browser=args.open_browser)
        return 1
```

Add the handler function near the other module-level helpers at the bottom of `cli.py`:

```python
def _mutation_report(runs_dir: Path, output: Path, *, open_browser: bool) -> int:
    from sqlproof.mutation.report import build_report, load_runs, render_html

    load_result = load_runs(runs_dir)
    for skipped in load_result.skipped:
        print(f"warning: skipped {skipped.path}: {skipped.reason}", file=sys.stderr)
    html_text = render_html(build_report(load_result))
    output.write_text(html_text, encoding="utf-8")
    print(f"wrote {output} ({len(load_result.runs)} run(s))")
    if open_browser:
        import webbrowser

        webbrowser.open(output.resolve().as_uri())
    return 0
```

(`Path` and `sys` are already imported at the top of `cli.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_smoke.py -v`
Expected: PASS (both new tests pass; the existing smoke tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/cli.py tests/unit/test_cli_smoke.py
git commit -m "feat(cli): add 'sqlproof mutation report' subcommand"
```

---

## Task 10: `mutant_id` stability regression test + `.gitignore`

**Files:**
- Create: `tests/unit/test_mutation_id_stability.py`
- Modify: `.gitignore`
- Test: the new file itself

- [ ] **Step 1: Write the test**

Create `tests/unit/test_mutation_id_stability.py`:

```python
from __future__ import annotations

from sqlproof.mutation.apply import prepare_mutants
from sqlproof.mutation.model import Mutant, MutationSet, Replace


def _mutation_set() -> MutationSet:
    return MutationSet(
        mutants=(
            Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),
        )
    )


def test_mutant_id_is_stable_across_schema_formatting() -> None:
    # Same logical function, different surrounding whitespace/formatting.
    compact = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"
    spaced = """
        CREATE   FUNCTION f()
          RETURNS int
          LANGUAGE sql
          AS $$   SELECT    1   $$;
    """
    id_compact = prepare_mutants(_mutation_set(), compact)[0].mutant_id
    id_spaced = prepare_mutants(_mutation_set(), spaced)[0].mutant_id
    assert id_compact == id_spaced


def test_distinct_mutations_get_distinct_ids() -> None:
    schema = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 + 1 $$;"
    set_a = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1 + 1", "2"),)),)
    )
    set_b = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1 + 1", "3"),)),)
    )
    id_a = prepare_mutants(set_a, schema)[0].mutant_id
    id_b = prepare_mutants(set_b, schema)[0].mutant_id
    assert id_a != id_b
```

- [ ] **Step 2: Run the test to verify the assumption holds**

Run: `uv run pytest tests/unit/test_mutation_id_stability.py -v`
Expected: PASS. (This pins existing `prepare_mutants` behaviour that the report now depends on. If `test_mutant_id_is_stable_across_schema_formatting` fails, the report's cross-run keying assumption is broken — stop and revisit before relying on `mutant_id` as identity.)

- [ ] **Step 3: Add the `.gitignore` note**

In `.gitignore`, add a line below the existing `.superpowers/` entry:

```
# Local mutation run artifacts (commit deliberately if you want shared history)
.sqlproof/
```

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_mutation_id_stability.py .gitignore
git commit -m "test(mutation): pin mutant_id stability; ignore .sqlproof artifacts"
```

---

## Task 11: Full verification + docs touch-up

**Files:**
- Modify: `README.md` (mutation section, if present) — optional doc note
- No test changes

- [ ] **Step 1: Run the unit suite with the coverage gate**

Run: `uv run pytest tests/unit -q` then `uv run pytest --cov=sqlproof --cov-fail-under=95`
Expected: PASS — all unit tests green (including every new test file) and coverage stays at or above the 95% gate. If the new modules drop coverage below 95%, add targeted tests for the uncovered branches before proceeding.

- [ ] **Step 2: Run type and lint checks (canonical commands from CONTRIBUTING.md)**

Run: `uv run ruff check src/ tests/ && uv run pyright && uv run mypy src/sqlproof/`
Expected: no errors.

- [ ] **Step 3: End-to-end smoke of the CLI against a real artifact dir**

Run:
```bash
uv run python - <<'PY'
import tempfile, pathlib, json
from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.result import MutantOutcome
from sqlproof.mutation.persist import save_run
d = pathlib.Path(tempfile.mkdtemp())
save_run(
    RunArtifact("id1","2026-06-11T10:00:00Z",5.0,"0.9.0","abc",False,42,"sha256:s1",("tests/",),
        (MutantOutcome("s1","f","drop x","survived",0,42,None,0.5),)),
    artifact_dir=d,
)
print("runs dir:", d)
PY
```
Then render: `uv run sqlproof mutation report --runs-dir <printed dir> --output /tmp/report.html`
Expected: prints `wrote /tmp/report.html (1 run(s))`; opening the file shows the score chart, the `f` target row, and the `s1` survivor with its repro command.

- [ ] **Step 4: Optional — add a short usage note to README**

If `README.md` has a mutation-testing section, add a couple of lines: pass `artifact_dir=` to `run_mutation_tests` to persist runs, then `sqlproof mutation report --runs-dir <dir> --open` to view the dashboard. Keep it consistent with the existing README voice.

- [ ] **Step 5: Commit (if README changed)**

```bash
git add README.md
git commit -m "docs: note mutation report usage"
```

- [ ] **Step 6: Push the branch**

```bash
git push
```
Expected: branch `docs/mutation-dashboard-design-draft` updated on origin; PR #95 now carries the implementation.

---

## Self-Review

**Spec coverage:**
- Run artifact format (schema_version, mutant_id, duration_s, git, fingerprint, append-only) → Tasks 1–5.
- `sqlproof mutation report` CLI with `--runs-dir`/`--output`/`--open` → Task 9.
- Score = (killed + unexpected_kill) / (non-error, non-expected_survivor); errors surfaced → Task 7 (`_score`) + render run-log errored count.
- Survivor new-vs-known keyed by `mutant_id` → Task 7 (`_build_latest_survivors`).
- Self-contained HTML, inline SVG, no CDN, embedded JSON blob → Task 8 + render tests.
- Trend-first layout (chart → per-target → survivors → run log) → Task 8 section order.
- Aggregation separate from rendering → `aggregate.py` vs `render.py`.
- Edge cases: no runs (Task 8 empty page), corrupt/unknown-version skipped with warning (Task 6 + Task 9 stderr), schema drift annotation (Task 7 `schema_changed` + Task 8 chart/run-log), disappearing targets (Task 7 `_build_targets` only iterates present targets), disappearing survivors (Task 7 derives survivors from latest run only), errored mutants excluded from denominator but counted (Task 7) → all covered.
- Testing strategy: aggregation unit tests (Tasks 6–7), artifact round-trip (Tasks 3–4), mutant_id stability (Task 10), HTML self-contained smoke (Task 8), CLI exit codes + empty dir (Task 9) → all covered.
- `.gitignore` note for `.sqlproof/` → Task 10.

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to" — every code step shows complete code. The only conditional instruction is Task 6 Step 3's note to write `__init__.py` without `render_html` until Task 8, which is then resolved explicitly in Task 8 Step 5.

**Type consistency:** `MutantOutcome.duration_s` (Task 1) is read in artifact serialization (Task 3), runner (Task 2), and aggregate (Task 7). `RunArtifact` fields (Task 3) match what `save_run` (Task 4), `run_mutation_tests` (Task 5), and `load_runs`/`build_report` (Tasks 6–7) construct and read. View-model dataclasses (`RunSummary`, `TargetSummary`, `SurvivorEntry`, `TrendPoint`, `ReportData`, `LoadResult`, `SkippedFile`) defined in `aggregate.py` are the exact names imported by `render.py` and its tests (Task 8). `load_runs` returns `LoadResult`, consumed by `build_report` everywhere. CLI imports `build_report`, `load_runs`, `render_html` from `sqlproof.mutation.report` (Task 9), all exported in Task 8 Step 5.
