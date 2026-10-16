"""Typed queries must preserve PostgreSQL values and report incomplete rows."""

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import NotRequired, TypedDict

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof.client import PostgresSqlProofClient
from sqlproof.exceptions import SqlProofMappingError, SqlProofUsageError

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)


@dataclass
class OrderModel:
    id: int
    total: Decimal | None


class OrderRow(TypedDict):
    id: int
    total: Decimal | None
    note: NotRequired[str]


@settings(max_examples=30, deadline=None)
@given(
    identifier=st.integers(-(2**31), 2**31 - 1),
    total=st.one_of(st.none(), st.decimals(-9999, 9999, places=2)),
)
@example(identifier=-(2**31), total=None)
def test_typed_queries_preserve_database_values_and_column_names(identifier, total) -> None:
    """Oracle: the input values, including SQL NULL and exact Decimal amounts."""
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        client = PostgresSqlProofClient(conn)
        sql = "SELECT %s::numeric AS total, %s::integer AS id, 'ignored' AS extra"
        assert client.query_typed(sql, OrderModel, total, identifier) == [
            OrderModel(id=identifier, total=total)
        ]
        assert client.query_typed(sql, OrderRow, total, identifier) == [
            {"id": identifier, "total": total, "extra": "ignored"}
        ]


@pytest.mark.parametrize("model", [OrderModel, OrderRow])
def test_typed_queries_report_a_missing_required_column(model) -> None:
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        client = PostgresSqlProofClient(conn)
        with pytest.raises(SqlProofMappingError, match="Missing required fields: total"):
            client.query_typed("SELECT 1 AS id", model)


def test_typed_queries_reject_an_unsupported_model() -> None:
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        client = PostgresSqlProofClient(conn)
        with pytest.raises(SqlProofUsageError, match="unsupported row model"):
            client.query_typed("SELECT 1 AS id", dict)


def test_typed_queries_and_scalars_return_empty_results_without_mapping_a_row() -> None:
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        client = PostgresSqlProofClient(conn)
        assert client.query_typed("SELECT 1 AS id WHERE false", OrderModel) == []
        assert client.scalar("SELECT 1 WHERE false") is None
