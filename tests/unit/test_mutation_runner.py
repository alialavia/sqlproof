from __future__ import annotations

import subprocess

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import PreparedMutant
from sqlproof.mutation.artifact import RunArtifact
from sqlproof.mutation.model import Mutant, Replace
from sqlproof.mutation.runner import LocalMutationRunner, _resolve_seed


def _prepared(mutant_id: str, *, expect_survives: bool = False) -> PreparedMutant:
    mutant = Mutant(
        target_kind="function",
        target_name="total_usage",
        ops=(Replace("a", "b"),),
        expect_survives=expect_survives,
        reason="dead branch" if expect_survives else None,
    )
    return PreparedMutant(mutant=mutant, mutant_id=mutant_id, ddl="CREATE OR REPLACE ...")


class FakeRunner(LocalMutationRunner):
    """Overrides every side-effecting method; records the call sequence."""

    def __init__(self, exit_codes: dict[str, int], **kwargs: object) -> None:
        super().__init__(
            database_url="postgresql://localhost/base",
            pytest_args=["tests/test_billing.py"],
            **kwargs,  # type: ignore[arg-type]
        )
        self.exit_codes = exit_codes
        self.calls: list[tuple[str, str]] = []

    def _create_clone(self, clone_name: str) -> str:
        self.calls.append(("create", clone_name))
        return f"postgresql://localhost/{clone_name}"

    def _apply_ddl(self, clone_dsn: str, ddl: str) -> None:
        self.calls.append(("apply", clone_dsn))

    def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
        self.calls.append(("pytest", clone_dsn))
        clone_name = clone_dsn.rsplit("/", 1)[1]
        mutant_id = clone_name.removeprefix("sqlproof_mutant_")
        return self.exit_codes[mutant_id], "output tail"

    def _drop_clone(self, clone_name: str) -> None:
        self.calls.append(("drop", clone_name))


def test_runner_maps_exit_codes_to_statuses() -> None:
    runner = FakeRunner({"m1": 1, "m2": 0})
    result = runner.run([_prepared("m1"), _prepared("m2")])
    assert [o.status for o in result.outcomes] == ["killed", "survived"]
    assert result.outcomes[0].mutant_id == "m1"


def test_runner_respects_expect_survives() -> None:
    runner = FakeRunner({"m1": 0})
    result = runner.run([_prepared("m1", expect_survives=True)])
    assert result.outcomes[0].status == "expected_survivor"


def test_clone_is_dropped_even_when_pytest_raises() -> None:
    class ExplodingRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            raise OSError("pytest binary vanished")

    runner = ExplodingRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    assert result.outcomes[0].status == "error"
    assert "pytest binary vanished" in (result.outcomes[0].detail or "")
    assert ("drop", "sqlproof_mutant_m1") in runner.calls


def test_runner_runs_clone_apply_pytest_drop_in_order() -> None:
    runner = FakeRunner({"m1": 1})
    runner.run([_prepared("m1")])
    kinds = [kind for kind, _ in runner.calls]
    assert kinds == ["create", "apply", "pytest", "drop"]


def test_parallel_runner_preserves_outcome_order() -> None:
    runner = FakeRunner({"m1": 1, "m2": 0, "m3": 1}, max_workers=3)
    result = runner.run([_prepared("m1"), _prepared("m2"), _prepared("m3")])
    assert [o.mutant_id for o in result.outcomes] == ["m1", "m2", "m3"]
    assert [o.status for o in result.outcomes] == ["killed", "survived", "killed"]


def test_pytest_command_includes_seed_flag_when_set() -> None:
    runner = FakeRunner({}, hypothesis_seed=42)
    command = runner._pytest_command()
    assert "--hypothesis-seed=42" in command
    assert "tests/test_billing.py" in command


def test_pytest_command_omits_seed_flag_by_default() -> None:
    runner = FakeRunner({})
    assert not any(arg.startswith("--hypothesis-seed") for arg in runner._pytest_command())


# ---------------------------------------------------------------------------
# Fix 1: subprocess timeout → "error" outcome, clone still dropped
# ---------------------------------------------------------------------------

def test_timeout_expired_yields_error_outcome_and_drops_clone() -> None:
    class TimeoutRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=1.0)

    runner = TimeoutRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    outcome = result.outcomes[0]
    assert outcome.status == "error"
    assert "TimeoutExpired" in (outcome.detail or "")
    assert ("drop", "sqlproof_mutant_m1") in runner.calls


# ---------------------------------------------------------------------------
# Fix 3: _resolve_seed helper
# ---------------------------------------------------------------------------

def test_resolve_seed_returns_given_value_unchanged() -> None:
    assert _resolve_seed(7) == 7


def test_resolve_seed_none_returns_int_in_range() -> None:
    result = _resolve_seed(None)
    assert isinstance(result, int)
    assert 0 <= result < 2**32


def test_resolve_seed_none_generates_distinct_values() -> None:
    # Extremely unlikely to collide; proves it's not a constant
    values = {_resolve_seed(None) for _ in range(10)}
    assert len(values) > 1


# ---------------------------------------------------------------------------
# Fix 4: unmasked root cause when finally-drop raises
# ---------------------------------------------------------------------------

def test_drop_exception_includes_root_cause_in_detail() -> None:
    class DoubleFaultRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            raise OSError("root cause")

        def _drop_clone(self, clone_name: str) -> None:
            # re-raise so __context__ is set on the new exception
            try:
                raise OSError("root cause")
            except OSError as err:
                raise RuntimeError("drop failed") from err

    runner = DoubleFaultRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    detail = result.outcomes[0].detail or ""
    assert "drop failed" in detail
    assert "root cause" in detail


# ---------------------------------------------------------------------------
# Fix 6: cheap unit tests for untested seams
# ---------------------------------------------------------------------------

def test_dsn_for_preserves_port_and_ssl() -> None:
    runner = LocalMutationRunner(
        database_url="postgresql://u:p@h:5433/base?sslmode=disable",
        pytest_args=[],
    )
    dsn = runner._dsn_for("clone1")
    assert "dbname=clone1" in dsn
    assert "port=5433" in dsn
    assert "sslmode=disable" in dsn


def test_dbname_on_dsn_without_dbname_raises() -> None:
    runner = LocalMutationRunner(
        database_url="postgresql://localhost/base",
        pytest_args=[],
    )
    with pytest.raises(SqlProofMutationError):
        runner._dbname("host=localhost")


def test_create_clone_failure_skips_drop_and_yields_error() -> None:
    class FailCreateRunner(FakeRunner):
        def _create_clone(self, clone_name: str) -> str:
            raise RuntimeError("create failed")

    runner = FailCreateRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    assert result.outcomes[0].status == "error"
    # _drop_clone must NOT have been called (clone was never created)
    assert not any(kind == "drop" for kind, _ in runner.calls)


def test_exit_code_2_yields_error_with_output_tail() -> None:
    runner = FakeRunner({"m1": 2})
    # FakeRunner._run_pytest returns "output tail" for exit code 2
    result = runner.run([_prepared("m1")])
    outcome = result.outcomes[0]
    assert outcome.status == "error"
    assert outcome.detail is not None
    assert "output tail" in outcome.detail


def test_exit_code_1_killed_has_none_detail() -> None:
    runner = FakeRunner({"m1": 1})
    result = runner.run([_prepared("m1")])
    assert result.outcomes[0].detail is None


def test_exit_code_0_survived_has_output_tail() -> None:
    runner = FakeRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    outcome = result.outcomes[0]
    assert outcome.status == "survived"
    assert outcome.detail == "output tail"


def test_long_output_is_truncated_to_last_2000_chars() -> None:
    long_output = "x" * 3000

    class LongOutputRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            self.calls.append(("pytest", clone_dsn))
            return 0, long_output

    runner = LongOutputRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    detail = result.outcomes[0].detail or ""
    assert len(detail) == 2000
    assert detail == "x" * 2000


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


def test_run_mutation_tests_returns_result_even_if_save_fails(tmp_path, monkeypatch) -> None:
    import pytest

    from sqlproof.mutation import runner as runner_module
    from sqlproof.mutation.model import Mutant, MutationSet, Replace
    from sqlproof.mutation.result import MutantOutcome, MutationResult

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
        encoding="utf-8",
    )

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

    def boom(artifact, *, artifact_dir):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(runner_module.LocalMutationRunner, "run", fake_run)
    monkeypatch.setattr(runner_module, "save_run", boom)

    mutations = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),)
    )
    with pytest.warns(UserWarning, match="artifact could not be written"):
        result = runner_module.run_mutation_tests(
            mutations,
            schema_file=schema_file,
            database_url="postgresql://localhost/base",
            pytest_args=["tests/"],
            artifact_dir=tmp_path / "runs",
        )
    # The mutation result must survive the save failure.
    assert result.outcomes[0].status == "killed"


def test_run_mutation_tests_degrades_fingerprint_on_error(tmp_path, monkeypatch) -> None:
    import json

    from sqlproof.mutation import runner as runner_module
    from sqlproof.mutation.model import Mutant, MutationSet, Replace
    from sqlproof.mutation.result import MutantOutcome, MutationResult

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
        encoding="utf-8",
    )

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

    def boom(_schema):  # type: ignore[no-untyped-def]
        raise ValueError("fingerprint exploded")

    monkeypatch.setattr(runner_module.LocalMutationRunner, "run", fake_run)
    monkeypatch.setattr(runner_module, "compute_fingerprint", boom)

    mutations = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),)
    )
    runs_dir = tmp_path / "runs"
    runner_module.run_mutation_tests(
        mutations,
        schema_file=schema_file,
        database_url="postgresql://localhost/base",
        pytest_args=["tests/"],
        artifact_dir=runs_dir,
    )
    from sqlproof.mutation.artifact import RunArtifact

    artifact = RunArtifact.from_json_dict(
        json.loads(next(runs_dir.glob("*.json")).read_text(encoding="utf-8"))
    )
    assert artifact.schema_fingerprint is None


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
    runner_module.run_mutation_tests(
        mutations,
        schema_file=schema_file,
        database_url="postgresql://localhost/base",
        pytest_args=["tests/"],
    )
    assert not list(tmp_path.glob("**/*.json"))
