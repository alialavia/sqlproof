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
    # What a re-fit and a re-run need (Ruling AQ). `baseline` is the fixed
    # per-call cost the fit subtracted: `fit_exponent(segment, baseline)`
    # over each plan segment of `points` reproduces `regimes`. The rest are
    # the parameters the sweep ran with. `run_sweep` always fills them; the
    # defaults only keep hand-built results constructible.
    baseline: int | None = None
    seed: int | None = None
    max_factor: int | None = None
    min_points: int | None = None
    probe_timeout_s: float | None = None
    # How each argument position was chosen (`args.argument_policy`): a
    # built-in resolver's kind and column, "callable", or "literal".
    argument_policy: Sequence[Mapping[str, object]] = ()

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
        """Did a sort or hash spill to disk at or below `rows` total rows?

        True when a spill was observed at or below `rows`, whatever range
        was measured. Otherwise the answer is known only up to the
        largest total_rows the sweep measured: False within that range,
        and beyond it this raises `SqlProofScaleError` rather than answer
        "no spill" for rows nobody measured (Ruling AP) -- the same
        refusal `rows_before_timeout` makes rather than extrapolate.
        """
        point = self.spill_point_rows
        if point is not None and point <= rows:
            return True
        measured = max((p.total_rows for p in self.points), default=0)
        if rows > measured:
            msg = (
                f"Cannot say whether {self.function} spills at or below "
                f"{rows:,} total rows: the sweep measured up to {measured:,} "
                "total rows and saw no spill there. Raising max_factor extends "
                "the measured range (raise min_points too: the ladder also "
                "stops as soon as the fit accepts)."
            )
            raise SqlProofScaleError(msg)
        return False

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
