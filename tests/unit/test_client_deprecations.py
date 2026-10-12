import pytest

from sqlproof import client


def test_the_old_psycopg_client_name_warns_and_returns_the_postgres_client() -> None:
    with pytest.warns(DeprecationWarning, match=r"renamed to PostgresSqlProofClient"):
        old = client.PsycopgSqlProofClient  # pyright: ignore[reportAttributeAccessIssue]

    assert old is client.PostgresSqlProofClient


def test_importing_the_old_psycopg_client_name_points_to_sqlproof_postgres() -> None:
    with pytest.warns(DeprecationWarning, match=r"sqlproof\.postgres\.connect"):
        from sqlproof.client import (
            PsycopgSqlProofClient,  # noqa: F401  # pyright: ignore[reportAttributeAccessIssue]
        )


def test_unknown_client_attributes_still_raise_attribute_error() -> None:
    with pytest.raises(AttributeError, match="NoSuchClient"):
        client.NoSuchClient  # noqa: B018  # pyright: ignore[reportAttributeAccessIssue]
