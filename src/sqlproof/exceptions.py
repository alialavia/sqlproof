from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SqlProofError(Exception):
    """Base for all SqlProof errors."""


class SqlProofUsageError(SqlProofError):
    """Caller misuse: invalid sizes, conflicting decorators, ambiguous types, etc."""


class SqlProofSchemaError(SqlProofError):
    """Schema parsing or introspection failure."""


class CircularDependencyError(SqlProofSchemaError):
    """FK cycle between distinct tables."""


class SqlProofGenerationError(SqlProofError):
    """Data generation exhausted retry budget for assume-and-retry constraints."""


class SqlProofMappingError(SqlProofError):
    """query_typed could not map a row to the requested model."""


class SqlProofTimeoutError(SqlProofError):
    """A property run exceeded its timeout."""


@dataclass(slots=True)
class SqlProofPropertyFailure(SqlProofError):
    """The property was falsified."""

    message: str
    counterexample: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class SqlProofContainerError(SqlProofError):
    """testcontainers startup, container died mid-run, etc."""


class SqlProofMutationError(SqlProofError):
    """Mutation testing: bad mutant definition, apply failure, or surviving mutants."""


class UnhonoredCheckWarning(UserWarning):
    """A CHECK constraint sqlproof cannot interpret at generation time.

    Generated rows may violate it, in which case Postgres rejects the
    INSERT with a CheckViolation. Supply a ``columns={...}`` override
    for the affected column(s) to generate valid values explicitly.
    """
