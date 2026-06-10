from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlproof.exceptions import SqlProofMutationError


def _check_survivor_fields(expect_survives: bool, reason: str | None) -> None:
    if expect_survives and not reason:
        msg = "expect_survives=True requires a reason= explaining the accepted survivor."
        raise SqlProofMutationError(msg)
    if reason and not expect_survives:
        msg = "reason= is only meaningful with expect_survives=True."
        raise SqlProofMutationError(msg)


@dataclass(frozen=True, slots=True)
class Replace:
    """Replace exactly one occurrence of `old` with `new` in the target body."""

    old: str
    new: str
    expect_survives: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.old == self.new:
            msg = f"Replace is a no-op: old and new are both {self.old!r}."
            raise SqlProofMutationError(msg)
        _check_survivor_fields(self.expect_survives, self.reason)

    def describe(self) -> str:
        return f"replace {self.old!r} -> {self.new!r}"

    def to_dict(self) -> dict[str, Any]:
        return {"op": "replace", "old": self.old, "new": self.new}


@dataclass(frozen=True, slots=True)
class Drop:
    """Delete exactly one occurrence of `pattern` from the target body."""

    pattern: str
    expect_survives: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        _check_survivor_fields(self.expect_survives, self.reason)

    def describe(self) -> str:
        return f"drop {self.pattern!r}"

    def to_dict(self) -> dict[str, Any]:
        return {"op": "drop", "pattern": self.pattern}


Op = Replace | Drop


def _bare(op: Op) -> Op:
    """Strip authoring-sugar flags; the Mutant carries them instead."""
    if isinstance(op, Replace):
        return Replace(op.old, op.new)
    return Drop(op.pattern)


def _op_from_dict(data: dict[str, Any]) -> Op:
    kind = data.get("op")
    if kind == "replace":
        return Replace(data["old"], data["new"])
    if kind == "drop":
        return Drop(data["pattern"])
    msg = f"Unknown mutation op kind: {kind!r}."
    raise SqlProofMutationError(msg)


@dataclass(frozen=True, slots=True)
class Mutant:
    """One deliberate bug: a target plus ordered text operations on its body."""

    target_kind: Literal["function"]
    target_name: str
    ops: tuple[Op, ...]
    expect_survives: bool = False
    reason: str | None = None

    def describe(self) -> str:
        rendered = "; ".join(op.describe() for op in self.ops)
        return f"{self.target_name}: {rendered}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": {"kind": self.target_kind, "name": self.target_name},
            "ops": [op.to_dict() for op in self.ops],
            "expect_survives": self.expect_survives,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mutant:
        target = data["target"]
        if target["kind"] != "function":
            msg = f"Unknown mutant target kind: {target['kind']!r}."
            raise SqlProofMutationError(msg)
        return cls(
            target_kind="function",
            target_name=target["name"],
            ops=tuple(_op_from_dict(op) for op in data["ops"]),
            expect_survives=bool(data.get("expect_survives", False)),
            reason=data.get("reason"),
        )


@dataclass(frozen=True, slots=True)
class MutationSet:
    mutants: tuple[Mutant, ...]

    @classmethod
    def for_function(cls, name: str, ops: Sequence[Op]) -> MutationSet:
        """One mutant per op against the named function's body."""
        if not ops:
            msg = f"MutationSet.for_function({name!r}) needs at least one op."
            raise SqlProofMutationError(msg)
        mutants = tuple(
            Mutant(
                target_kind="function",
                target_name=name,
                ops=(_bare(op),),
                expect_survives=op.expect_survives,
                reason=op.reason,
            )
            for op in ops
        )
        return cls(mutants)

    def __add__(self, other: MutationSet) -> MutationSet:
        return MutationSet(self.mutants + other.mutants)

    def to_dict(self) -> dict[str, Any]:
        return {"mutants": [mutant.to_dict() for mutant in self.mutants]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MutationSet:
        return cls(tuple(Mutant.from_dict(item) for item in data["mutants"]))
