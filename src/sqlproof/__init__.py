from __future__ import annotations

from typing import TYPE_CHECKING

from sqlproof._version import __version__

if TYPE_CHECKING:
    from sqlproof.config import ExternalTableSpec, SqlProofConfig
    from sqlproof.core import SqlProof
    from sqlproof.runners import sqlproof

__all__ = ["ExternalTableSpec", "SqlProof", "SqlProofConfig", "__version__", "sqlproof"]


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
    raise AttributeError(name)
