from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FunctionCall:
    sql: str
    overload_name: str
