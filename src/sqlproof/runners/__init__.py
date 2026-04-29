from __future__ import annotations

from sqlproof.runners.migration import migration
from sqlproof.runners.overload import function_overloads
from sqlproof.runners.property import Check, sqlproof
from sqlproof.runners.rls import rls
from sqlproof.runners.stateful import stateful

sqlproof.stateful = stateful  # type: ignore[attr-defined]
sqlproof.migration = migration  # type: ignore[attr-defined]
sqlproof.rls = rls  # type: ignore[attr-defined]
sqlproof.function_overloads = function_overloads  # type: ignore[attr-defined]

__all__ = ["Check", "function_overloads", "migration", "rls", "sqlproof", "stateful"]
