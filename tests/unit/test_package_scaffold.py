from __future__ import annotations

import pytest

from sqlproof import SqlProof, SqlProofConfig, __version__
from sqlproof.exceptions import (
    CircularDependencyError,
    SqlProofContainerError,
    SqlProofError,
    SqlProofGenerationError,
    SqlProofMappingError,
    SqlProofPropertyFailure,
    SqlProofSchemaError,
    SqlProofTimeoutError,
    SqlProofUsageError,
)


def test_public_api_exports_version_config_and_sqlproof() -> None:
    assert isinstance(__version__, str)
    assert __version__
    assert SqlProof.__name__ == "SqlProof"
    assert SqlProofConfig.__name__ == "SqlProofConfig"


@pytest.mark.parametrize(
    "error_type",
    [
        SqlProofUsageError,
        SqlProofSchemaError,
        CircularDependencyError,
        SqlProofGenerationError,
        SqlProofMappingError,
        SqlProofTimeoutError,
        SqlProofPropertyFailure,
        SqlProofContainerError,
    ],
)
def test_all_public_errors_share_base_class(error_type: type[Exception]) -> None:
    assert issubclass(error_type, SqlProofError)


def test_circular_dependency_is_schema_error() -> None:
    assert issubclass(CircularDependencyError, SqlProofSchemaError)


def test_config_requires_exactly_one_schema_source() -> None:
    SqlProofConfig(schema_file="schema.sql")
    SqlProofConfig(connection_string="postgresql://localhost/postgres")

    with pytest.raises(SqlProofUsageError, match="Exactly one"):
        SqlProofConfig()

    with pytest.raises(SqlProofUsageError, match="Exactly one"):
        SqlProofConfig(
            connection_string="postgresql://localhost/postgres",
            schema_file="schema.sql",
        )
