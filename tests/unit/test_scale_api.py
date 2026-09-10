"""`scale_analysis`'s wiring -- no database needed.

A `SqlProof` built from a schema file (rather than a connection_string)
has nowhere for the sweep to run TRUNCATE/COPY/EXPLAIN against, so
`scale_analysis` must refuse before ever attempting a connection.

The rest of this module pins the artifact-writing wiring (Task 10) by
faking out `psycopg.connect` and `run_sweep`, so it runs without a live
database too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from sqlproof.core import SqlProof
from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale import scale_analysis
from sqlproof.scale.result import ScaleResult

SCHEMA_SQL = "CREATE TABLE widgets (id bigint PRIMARY KEY, name text NOT NULL);"


def test_scale_analysis_without_a_connection_string_raises_usage_error(
    tmp_path: Path,
) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    proof = SqlProof.from_schema_file(schema_file)

    with pytest.raises(SqlProofUsageError, match="connection_string"):
        scale_analysis(proof, "public.f", sizes={"widgets": 10})


def _canned_result() -> ScaleResult:
    return ScaleResult(
        points=(),
        regimes=(),
        plan_flips=(),
        truncated=False,
        function="public.f",
        sizes={"widgets": 10},
    )


class _FakeConnection:
    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _fake_connect(dsn: str, **kwargs: Any) -> _FakeConnection:
    return _FakeConnection()


def _fake_run_sweep(
    conn: Any, schema_info: Any, function: str, *, sizes: Any, args: Any = (), **kwargs: Any
) -> ScaleResult:
    return _canned_result()


@dataclass
class _StubConfig:
    connection_string: str | None


@dataclass
class _StubProof:
    config: _StubConfig
    schema_info: Any
    schema_fingerprint: str


def _stub_proof(fingerprint: str = "fp-123") -> SqlProof:
    # A duck-typed stand-in for SqlProof: `scale_analysis` only reads
    # `.config.connection_string`, `.schema_info` and `.schema_fingerprint`.
    # `cast` satisfies the type checker without a real (DB-backed) SqlProof.
    return cast(
        SqlProof,
        _StubProof(
            config=_StubConfig(connection_string="postgresql://stub/db"),
            schema_info=None,
            schema_fingerprint=fingerprint,
        ),
    )


def test_scale_analysis_writes_artifact_with_the_proofs_schema_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sqlproof.scale.psycopg.connect", _fake_connect)
    monkeypatch.setattr("sqlproof.scale.run_sweep", _fake_run_sweep)

    scale_analysis(
        _stub_proof(fingerprint="fp-abc"),
        "public.f",
        sizes={"widgets": 10},
        artifact_dir=tmp_path,
    )

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["schema_fingerprint"] == "fp-abc"


def test_scale_analysis_with_artifact_dir_none_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sqlproof.scale.psycopg.connect", _fake_connect)
    monkeypatch.setattr("sqlproof.scale.run_sweep", _fake_run_sweep)
    monkeypatch.chdir(tmp_path)

    scale_analysis(_stub_proof(), "public.f", sizes={"widgets": 10}, artifact_dir=None)

    assert not (tmp_path / ".sqlproof").exists()


def test_scale_analysis_default_artifact_dir_is_dot_sqlproof_scale_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sqlproof.scale.psycopg.connect", _fake_connect)
    monkeypatch.setattr("sqlproof.scale.run_sweep", _fake_run_sweep)
    monkeypatch.chdir(tmp_path)

    scale_analysis(_stub_proof(), "public.f", sizes={"widgets": 10})

    files = list((tmp_path / ".sqlproof" / "scale-runs").glob("*.json"))
    assert len(files) == 1


def test_scale_analysis_warns_and_still_returns_when_artifact_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sqlproof.scale.psycopg.connect", _fake_connect)
    monkeypatch.setattr("sqlproof.scale.run_sweep", _fake_run_sweep)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.warns(UserWarning, match="artifact could not be written"):
        result = scale_analysis(
            _stub_proof(),
            "public.f",
            sizes={"widgets": 10},
            artifact_dir=blocker / "runs",
        )

    assert result.function == "public.f"
