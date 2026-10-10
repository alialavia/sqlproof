"""PostgreSQL range values used by the generators and database adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
        lower = "" if self.lower is None else _bound_text(self.lower)
        upper = "" if self.upper is None else _bound_text(self.upper)
        return f"{self.bounds[0]}{lower},{upper}{self.bounds[1]}"


def _bound_text(value: Any) -> str:
    """Render one bound. The date/datetime min and max sentinels that infinite
    bounds are read back as are written out as infinity again, so a range
    read from PostgreSQL and written back keeps its open end."""
    if isinstance(value, datetime):
        naive = value.replace(tzinfo=None)
        if naive == datetime.max:
            return "infinity"
        if naive == datetime.min:
            return "-infinity"
    elif isinstance(value, date):
        if value == date.max:
            return "infinity"
        if value == date.min:
            return "-infinity"
    return quote_field(str(value))
