"""PostgreSQL range values used by the generators and database adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def quote_field(value: str) -> str:
    """Quote one array, record or range field in PostgreSQL text format."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class Range:
    lower: Any = None
    upper: Any = None
    bounds: str = "[)"
    empty: bool = False

    def __post_init__(self) -> None:
        if self.bounds not in {"[)", "[]", "()", "(]"}:
            raise ValueError("Range bounds must be [), [], () or (].")

    @property
    def isempty(self) -> bool:
        return self.empty

    def __str__(self) -> str:
        if self.empty:
            return "empty"
        lower = "" if self.lower is None else quote_field(str(self.lower))
        upper = "" if self.upper is None else quote_field(str(self.upper))
        return f"{self.bounds[0]}{lower},{upper}{self.bounds[1]}"
