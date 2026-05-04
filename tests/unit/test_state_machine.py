from __future__ import annotations

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.stateful import invariant, rule

from sqlproof import SqlProof
from sqlproof.exceptions import SqlProofUsageError
from sqlproof.testing import SqlProofStateMachine


@pytest.fixture
def proof(tmp_path) -> SqlProof:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text("CREATE TABLE items (id SERIAL PRIMARY KEY);", encoding="utf-8")
    return SqlProof.from_schema_file(schema_file)


def test_direct_instantiation_without_proof_binding_is_rejected() -> None:
    class BareMachine(SqlProofStateMachine):
        @rule()
        def noop(self) -> None:
            pass

    with pytest.raises(SqlProofUsageError, match="run_state_machine"):
        BareMachine()


def test_run_state_machine_rejects_non_subclasses(proof: SqlProof) -> None:
    class NotAMachine:
        pass

    with pytest.raises(SqlProofUsageError, match="subclass of SqlProofStateMachine"):
        proof.run_state_machine(NotAMachine)  # type: ignore[arg-type]


def test_run_state_machine_executes_rules_and_invariants(proof: SqlProof) -> None:
    on_setup_calls: list[int] = []
    rule_calls: list[int] = []
    invariant_calls: list[int] = []

    class Machine(SqlProofStateMachine):
        def on_setup(self) -> None:
            on_setup_calls.append(1)
            assert self.db is not None

        @rule()
        def step(self) -> None:
            rule_calls.append(1)

        @invariant()
        def db_is_present(self) -> None:
            invariant_calls.append(1)
            assert self.db is not None

    proof.run_state_machine(
        Machine,
        settings=settings(
            max_examples=4,
            stateful_step_count=3,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        ),
    )

    assert len(on_setup_calls) >= 4
    assert len(rule_calls) >= 4
    assert len(invariant_calls) >= 4


def test_run_state_machine_isolates_state_between_examples(proof: SqlProof) -> None:
    starting_states: list[int] = []

    class Machine(SqlProofStateMachine):
        def on_setup(self) -> None:
            starting_states.append(getattr(self, "_counter", 0))
            self._counter = 0

        @rule()
        def increment(self) -> None:
            self._counter += 1

        @invariant()
        def counter_non_negative(self) -> None:
            assert getattr(self, "_counter", 0) >= 0

    proof.run_state_machine(
        Machine,
        settings=settings(
            max_examples=3,
            stateful_step_count=4,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        ),
    )

    assert len(starting_states) >= 3
    assert all(state == 0 for state in starting_states)


def test_enter_lifetime_is_scoped_to_each_example(proof: SqlProof) -> None:
    from contextlib import contextmanager

    enter_count = 0
    exit_count = 0

    @contextmanager
    def tracking_resource():
        nonlocal enter_count, exit_count
        enter_count += 1
        try:
            yield "resource"
        finally:
            exit_count += 1

    class Machine(SqlProofStateMachine):
        def on_setup(self) -> None:
            self.resource = self.enter(tracking_resource())

        @rule()
        def use_resource(self) -> None:
            assert self.resource == "resource"

    proof.run_state_machine(
        Machine,
        settings=settings(
            max_examples=3,
            stateful_step_count=2,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        ),
    )

    assert enter_count >= 3
    assert enter_count == exit_count


def test_failing_invariant_surfaces_through_run_state_machine(proof: SqlProof) -> None:
    class Machine(SqlProofStateMachine):
        @rule()
        def step(self) -> None:
            pass

        @invariant()
        def always_fails(self) -> None:
            raise AssertionError("planted failure")

    with pytest.raises(AssertionError, match="planted failure"):
        proof.run_state_machine(
            Machine,
            settings=settings(
                max_examples=2,
                deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture],
            ),
        )
