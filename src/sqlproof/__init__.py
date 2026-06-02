from __future__ import annotations

from typing import TYPE_CHECKING

from sqlproof._version import __version__

if TYPE_CHECKING:
    from sqlproof.config import ExternalTableSpec, SqlProofConfig
    from sqlproof.core import SqlProof
    from sqlproof.runners import sqlproof
    from sqlproof.surface import DriftReport, SurfaceRegistry, SurfaceRegistryDrift

__all__ = [
    "DriftReport",
    "ExternalTableSpec",
    "SqlProof",
    "SqlProofConfig",
    "SurfaceRegistry",
    "SurfaceRegistryDrift",
    "__version__",
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
    raise AttributeError(name)
