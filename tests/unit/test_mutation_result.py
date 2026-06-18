from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.result import MutantOutcome, MutationResult, outcome_for_exit_code


def _outcome(status: str, *, mutant_id: str = "abc123") -> MutantOutcome:
    return MutantOutcome(
        mutant_id=mutant_id,
        target="total_usage",
        description=f"total_usage: drop {mutant_id!r}",
        status=status,  # type: ignore[arg-type]
        pytest_exit_code=0,
        hypothesis_seed=None,
        detail=None,
    )


@pytest.mark.parametrize(
    ("exit_code", "expect_survives", "status"),
    [
        (1, False, "killed"),
        (0, False, "survived"),
        (0, True, "expected_survivor"),
        (1, True, "unexpected_kill"),
        (2, False, "error"),
        (3, False, "error"),
        (4, False, "error"),
        (5, False, "error"),  # no tests collected proves nothing
    ],
)
def test_exit_code_mapping(exit_code: int, expect_survives: bool, status: str) -> None:
    outcome = outcome_for_exit_code(
        mutant_id="abc",
        target="f",
        description="f: drop 'x'",
        expect_survives=expect_survives,
        exit_code=exit_code,
        hypothesis_seed=7,
        detail="tail",
    )
    assert outcome.status == status
    assert outcome.pytest_exit_code == exit_code
    assert outcome.hypothesis_seed == 7


def test_assert_no_survivors_passes_when_all_killed() -> None:
    result = MutationResult(outcomes=(_outcome("killed"), _outcome("expected_survivor")))
    result.assert_no_survivors()  # must not raise


def test_assert_no_survivors_raises_on_survivor_with_description() -> None:
    result = MutationResult(outcomes=(_outcome("survived"),))
    with pytest.raises(SqlProofMutationError, match="drop"):
        result.assert_no_survivors()


def test_assert_no_survivors_raises_on_error_outcomes() -> None:
    result = MutationResult(outcomes=(_outcome("error"),))
    with pytest.raises(SqlProofMutationError, match="error"):
        result.assert_no_survivors()


def test_assert_no_survivors_message_shape() -> None:
    """Pin the exact header and per-line format: counts, exit code, seed, detail tail."""
    survivor = MutantOutcome(
        mutant_id="s1",
        target="total_usage",
        description="total_usage: drop 's1'",
        status="survived",
        pytest_exit_code=0,
        hypothesis_seed=7,
        detail="x" * 600,  # longer than 500-char tail
    )
    error = MutantOutcome(
        mutant_id="e1",
        target="total_usage",
        description="total_usage: drop 'e1'",
        status="error",
        pytest_exit_code=5,
        hypothesis_seed=None,
        detail=None,
    )
    result = MutationResult(outcomes=(survivor, error))
    with pytest.raises(SqlProofMutationError) as exc_info:
        result.assert_no_survivors()
    msg = str(exc_info.value)
    assert "1 survivor(s), 1 error(s):" in msg
    assert "(exit=0, seed=7)" in msg
    assert "(exit=5, seed=None)" in msg
    # detail tail: last 500 chars of "x" * 600
    assert "x" * 500 in msg


def test_unexpected_kill_does_not_fail_the_run() -> None:
    # Tests now cover what was declared dead code — good news, not failure.
    result = MutationResult(outcomes=(_outcome("unexpected_kill"),))
    result.assert_no_survivors()


def test_to_dict_is_json_serializable() -> None:
    import json

    result = MutationResult(outcomes=(_outcome("killed"), _outcome("survived")))
    assert json.loads(json.dumps(result.to_dict()))["outcomes"][1]["status"] == "survived"


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
