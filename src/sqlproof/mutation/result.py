from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlproof.exceptions import SqlProofMutationError

Status = Literal["killed", "survived", "expected_survivor", "unexpected_kill", "error"]


@dataclass(frozen=True, slots=True)
class MutantOutcome:
    mutant_id: str
    target: str
    description: str
    status: Status
    pytest_exit_code: int | None
    hypothesis_seed: int | None
    detail: str | None


def outcome_for_exit_code(
    *,
    mutant_id: str,
    target: str,
    description: str,
    expect_survives: bool,
    exit_code: int,
    hypothesis_seed: int | None,
    detail: str | None,
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
    )


@dataclass(frozen=True, slots=True)
class MutationResult:
    outcomes: tuple[MutantOutcome, ...]

    @property
    def survivors(self) -> tuple[MutantOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "survived")

    @property
    def errors(self) -> tuple[MutantOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "error")

    def assert_no_survivors(self) -> None:
        """Fail on surviving mutants AND on errored runs — a mutant whose
        suite run errored proves nothing about test strength."""
        problems = [*self.survivors, *self.errors]
        if not problems:
            return
        lines = [f"{len(problems)} mutant(s) not killed:"]
        for outcome in problems:
            lines.append(f"  [{outcome.status}] {outcome.description}")
            if outcome.detail:
                lines.append(f"    {outcome.detail.strip()[-500:]}")
        raise SqlProofMutationError("\n".join(lines))

    def to_dict(self) -> dict[str, Any]:
        return {"outcomes": [asdict(outcome) for outcome in self.outcomes]}
