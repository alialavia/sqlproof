from __future__ import annotations

from sqlproof.mutation.apply import PreparedMutant
from sqlproof.mutation.model import Mutant, Replace
from sqlproof.mutation.runner import LocalMutationRunner


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
