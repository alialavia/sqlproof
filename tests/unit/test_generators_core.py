from __future__ import annotations

from hypothesis import find, given, settings

from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.graph import dataset_strategy
from sqlproof.schema.model import CheckConstraint, Column, ForeignKey, PgType, SchemaInfo, Table


def test_integer_strategy_respects_postgres_bounds() -> None:
    column = Column(
        "value", PgType("scalar", "integer"), nullable=False, default=None, is_generated=False
    )

    @given(strategy_for_column(column))
    @settings(max_examples=25)
    def assert_bounds(value: int) -> None:
        assert -2_147_483_648 <= value <= 2_147_483_647

    assert_bounds()


def test_nullable_columns_can_generate_none() -> None:
    column = Column(
        "name", PgType("scalar", "text"), nullable=True, default=None, is_generated=False
    )

    assert find(strategy_for_column(column), lambda value: value is None) is None


def test_row_generation_refines_simple_range_check() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="orders",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column(
                        "total", PgType("scalar", "numeric", modifiers=(10, 2)), False, None, False
                    ),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(CheckConstraint("total >= 0"),),
            ),
        ),
    )

    @given(dataset_strategy(schema, sizes={"orders": 5}))
    @settings(max_examples=10)
    def assert_check_is_refined(dataset: dict[str, list[dict[str, object]]]) -> None:
        assert all(row["total"] >= 0 for row in dataset["orders"])

    assert_check_is_refined()


def test_row_generation_refines_in_set_and_length_checks() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="products",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column("status", PgType("scalar", "text"), False, None, False),
                    Column("code", PgType("scalar", "text"), False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(
                    CheckConstraint("status IN ('draft', 'active')"),
                    CheckConstraint("length(code) <= 4"),
                ),
            ),
        ),
    )

    @given(dataset_strategy(schema, sizes={"products": 5}))
    @settings(max_examples=10)
    def assert_check_is_refined(dataset: dict[str, list[dict[str, object]]]) -> None:
        assert {row["status"] for row in dataset["products"]} <= {"draft", "active"}
        assert all(len(row["code"]) <= 4 for row in dataset["products"])

    assert_check_is_refined()


def test_dataset_strategy_generates_unique_single_column_constraints() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="users",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column("email", PgType("scalar", "text"), False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(("email",),),
                check_constraints=(),
            ),
        ),
    )

    @given(dataset_strategy(schema, sizes={"users": 5}))
    @settings(max_examples=10)
    def assert_unique_values(dataset: dict[str, list[dict[str, object]]]) -> None:
        emails = [row["email"] for row in dataset["users"]]
        assert len(set(emails)) == len(emails)

    assert_unique_values()


def test_dataset_strategy_generates_unique_primary_keys_and_valid_foreign_keys() -> None:
    integer = PgType("scalar", "integer")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="customers",
                columns=(Column("id", integer, False, None, False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
            Table(
                schema="public",
                name="orders",
                columns=(
                    Column("id", integer, False, None, False),
                    Column("customer_id", integer, False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(
                    ForeignKey(("customer_id",), "customers", ("id",), "NO ACTION", "NO ACTION"),
                ),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    @given(dataset_strategy(schema, sizes={"customers": 3, "orders": 10}))
    @settings(max_examples=10)
    def assert_dataset_integrity(dataset: dict[str, list[dict[str, object]]]) -> None:
        customer_ids = {row["id"] for row in dataset["customers"]}

        assert len(customer_ids) == 3
        assert len({row["id"] for row in dataset["orders"]}) == 10
        assert all(row["customer_id"] in customer_ids for row in dataset["orders"])

    assert_dataset_integrity()
