from __future__ import annotations

from typing import TYPE_CHECKING

from sqlproof._version import __version__

if TYPE_CHECKING:
    from sqlproof.config import ExternalTableSpec, SqlProofConfig
    from sqlproof.core import SqlProof
    from sqlproof.mutation.model import Drop, MutationSet, Replace
    from sqlproof.mutation.result import MutantOutcome, MutationResult
    from sqlproof.mutation.runner import run_mutation_tests
    from sqlproof.runners import sqlproof
    from sqlproof.surface import DriftReport, SurfaceRegistry, SurfaceRegistryDrift

__all__ = [
    "DriftReport",
    "Drop",
    "ExternalTableSpec",
    "MutantOutcome",
    "MutationResult",
    "MutationSet",
    "Replace",
    "SqlProof",
    "SqlProofConfig",
    "SurfaceRegistry",
    "SurfaceRegistryDrift",
    "__version__",
    "run_mutation_tests",
    "sqlproof",
]


def __getattr__(name: str) -> object:
    if name == "SqlProof":
        from sqlproof.core import SqlProof

        return SqlProof
    if name == "SqlProofConfig":
        from sqlproof.config import SqlProofConfig

        return SqlProofConfig
    if name == "ExternalTableSpec":
        from sqlproof.config import ExternalTableSpec

        return ExternalTableSpec
    if name == "sqlproof":
        from sqlproof.runners import sqlproof

        return sqlproof
    if name in {"SurfaceRegistry", "SurfaceRegistryDrift", "DriftReport"}:
        from sqlproof import surface

        return getattr(surface, name)
    if name in {"MutationSet", "Replace", "Drop"}:
        from sqlproof.mutation import model

        return getattr(model, name)
    if name in {"MutationResult", "MutantOutcome"}:
        from sqlproof.mutation import result

        return getattr(result, name)
    if name == "run_mutation_tests":
        from sqlproof.mutation.runner import run_mutation_tests

        return run_mutation_tests
    raise AttributeError(name)
