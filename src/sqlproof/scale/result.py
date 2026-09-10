"""What a scale run reports, and what a test asserts against.

Every row-count figure is TOTAL ROWS ACROSS THE PROFILE --
`sum(sizes.values()) * factor` -- never one table's count. The sweep
scales a whole profile, so a single table's number would be ambiguous.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.fit import FitResult, PlanFlip, find_spill, project_rows_before_timeout
from sqlproof.scale.probe import ProbePoint


@dataclass(frozen=True, slots=True)
class ScaleResult:
    points: Sequence[ProbePoint]
    regimes: Sequence[FitResult]
    plan_flips: Sequence[PlanFlip]
    truncated: bool
    function: str
    sizes: Mapping[str, int]

    @property
    def exponent(self) -> float:
        """The complexity exponent of the final plan regime.

        Raises rather than returning None. `assert result.exponent < 1.5`
        against a None would fail with a TypeError that explains nothing;
        the reason the fit failed is what the caller actually needs.
        """
        if not self.regimes:
            msg = (
                f"No stable plan regime for {self.function}: the planner changed "
                f"strategy at every scale point ({len(self.plan_flips)} flips). "
                "The flips are the finding; no exponent is claimed."
            )
            raise SqlProofScaleError(msg)
        final = self.regimes[-1]
        if final.exponent is None:
            msg = (
                f"Could not fit a complexity exponent for {self.function}: "
                f"{final.reason}. Measured factors {final.from_factor}x-"
                f"{final.to_factor}x."
            )
            raise SqlProofScaleError(msg)
        return final.exponent

    @property
    def r_squared(self) -> float | None:
        return self.regimes[-1].r_squared if self.regimes else None

    @property
    def _spill(self) -> ProbePoint | None:
        return find_spill(self.points)

    @property
    def spill_point_rows(self) -> int | None:
        spill = self._spill
        return None if spill is None else spill.total_rows

    @property
    def factor_at_spill(self) -> int | None:
        spill = self._spill
        return None if spill is None else spill.factor

    def spills_below(self, rows: int) -> bool:
        """Did a sort or hash spill to disk at or below `rows` total rows?"""
        point = self.spill_point_rows
        return point is not None and point <= rows

    def rows_before_timeout(self, timeout_ms: float) -> tuple[int, int] | None:
        """Projected total rows at which the function crosses `timeout_ms`.

        MACHINE-DEPENDENT: derived from wall-clock on the machine that
        ran the sweep, unlike `exponent`. Returns a band, or None when
        projecting would be guessing.
        """
        if not self.regimes:
            return None
        return project_rows_before_timeout(
            self.points, self.regimes[-1], timeout_ms, truncated=self.truncated
        )
