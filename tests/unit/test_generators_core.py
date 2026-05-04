from __future__ import annotations

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample

from sqlproof.exceptions import SqlProofGenerationError
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


def test_dataset_strategy_omits_defaulted_columns_unless_overridden() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="settings",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column("status", PgType("scalar", "text"), False, "'active'::text", False),
                    Column("label", PgType("scalar", "text"), False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    dataset = find(
        dataset_strategy(
            schema,
            sizes={"settings": 1},
            columns={"settings.label": "generated"},
        ),
        lambda _: True,
    )

    assert dataset["settings"] == [{"id": 1, "label": "generated"}]

    overridden = find(
        dataset_strategy(
            schema,
            sizes={"settings": 1},
            columns={"settings.status": "inactive", "settings.label": "generated"},
        ),
        lambda _: True,
    )

    assert overridden["settings"] == [{"id": 1, "status": "inactive", "label": "generated"}]


def test_text_strategies_do_not_generate_postgres_invalid_codepoints() -> None:
    text_columns = [
        Column(
            "text_value",
            PgType("scalar", "text"),
            nullable=False,
            default=None,
            is_generated=False,
        ),
        Column(
            "varchar_value",
            PgType("scalar", "character varying", modifiers=(12,)),
            nullable=False,
            default=None,
            is_generated=False,
        ),
        Column(
            "char_value",
            PgType("scalar", "character"),
            nullable=False,
            default=None,
            is_generated=False,
        ),
    ]

    for column in text_columns:
        with pytest.raises(NoSuchExample):
            find(
                strategy_for_column(column),
                lambda value: "\x00" in value
                or any("\ud800" <= char <= "\udfff" for char in value),
            )


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


def test_row_generation_refines_postgres_any_array_check() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="brand_config",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column("pricing_model", PgType("scalar", "text"), True, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(
                    CheckConstraint(
                        "(pricing_model = ANY (ARRAY['subscription'::text, "
                        "'one_time'::text, 'freemium'::text]))"
                    ),
                ),
            ),
        ),
    )

    @given(dataset_strategy(schema, sizes={"brand_config": 5}))
    @settings(max_examples=10)
    def assert_check_is_refined(dataset: dict[str, list[dict[str, object]]]) -> None:
        assert {
            row["pricing_model"]
            for row in dataset["brand_config"]
            if row["pricing_model"] is not None
        } <= {"subscription", "one_time", "freemium"}

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


def test_dataset_strategy_accepts_shrinkable_table_size_strategies() -> None:
    integer = PgType("scalar", "integer")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="users",
                columns=(Column("id", integer, False, None, False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    minimal_dataset = find(
        dataset_strategy(schema, sizes={"users": st.integers(min_value=1, max_value=3)}),
        lambda dataset: len(dataset["users"]) >= 2,
    )

    assert len(minimal_dataset["users"]) == 2


def test_dataset_strategy_can_sample_foreign_keys_from_external_parent_rows() -> None:
    uuid_type = PgType("scalar", "uuid")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="projects",
                columns=(
                    Column("id", uuid_type, False, None, False),
                    Column("user_id", uuid_type, False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(
                    ForeignKey(
                        columns=("user_id",),
                        referenced_table="users",
                        referenced_columns=("id",),
                        on_delete="NO ACTION",
                        on_update="NO ACTION",
                        referenced_schema="auth",
                    ),
                ),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    dataset = find(
        dataset_strategy(
            schema,
            sizes={"projects": 3},
            external_parent_rows={"auth.users": [{"id": "external-user"}]},
        ),
        lambda _: True,
    )

    assert dataset == {
        "projects": [
            {"id": "00000000-0000-0000-0000-000000000001", "user_id": "external-user"},
            {"id": "00000000-0000-0000-0000-000000000002", "user_id": "external-user"},
            {"id": "00000000-0000-0000-0000-000000000003", "user_id": "external-user"},
        ]
    }


def test_dataset_strategy_generates_null_for_nullable_foreign_key_without_parents() -> None:
    uuid_type = PgType("scalar", "uuid")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="prompt_generation_sessions",
                columns=(Column("id", uuid_type, False, None, False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
            Table(
                schema="public",
                name="brand_prompts",
                columns=(
                    Column("id", uuid_type, False, None, False),
                    Column("generation_session_id", uuid_type, True, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(
                    ForeignKey(
                        ("generation_session_id",),
                        "prompt_generation_sessions",
                        ("id",),
                        "NO ACTION",
                        "NO ACTION",
                    ),
                ),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    with pytest.raises(NoSuchExample):
        find(
            dataset_strategy(
                schema,
                sizes={"prompt_generation_sessions": 0, "brand_prompts": 1},
            ),
            lambda dataset: dataset["brand_prompts"][0]["generation_session_id"] is not None,
        )


def test_dataset_strategy_errors_for_required_foreign_key_without_parents() -> None:
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

    with pytest.raises(SqlProofGenerationError, match=r"orders\.customer_id"):
        find(
            dataset_strategy(schema, sizes={"customers": 0, "orders": 1}),
            lambda _: True,
        )


def test_dataset_strategy_accepts_column_value_and_strategy_overrides() -> None:
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="users",
                columns=(
                    Column("id", PgType("scalar", "integer"), False, None, False),
                    Column("name", PgType("scalar", "text"), False, None, False),
                    Column("role", PgType("scalar", "text"), False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    dataset = find(
        dataset_strategy(
            schema,
            sizes={"users": 2},
            columns={
                "users.name": "Ripenn",
                "public.users.role": st.sampled_from(["admin"]),
            },
        ),
        lambda _: True,
    )

    assert dataset["users"] == [
        {"id": 1, "name": "Ripenn", "role": "admin"},
        {"id": 2, "name": "Ripenn", "role": "admin"},
    ]


def test_dataset_strategy_accepts_derived_column_overrides() -> None:
    integer = PgType("scalar", "integer")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="events",
                columns=(
                    Column("id", integer, False, None, False),
                    Column("sequence", integer, False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
            Table(
                schema="public",
                name="summaries",
                columns=(
                    Column("id", integer, False, None, False),
                    Column("event_count", integer, False, None, False),
                ),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    minimal_dataset = find(
        dataset_strategy(
            schema,
            sizes={"events": st.integers(min_value=1, max_value=3), "summaries": 1},
            columns={
                "events.sequence": lambda ctx: ctx.row_index,
                "summaries.event_count": lambda ctx: len(ctx.rows_by_table["events"]),
            },
        ),
        lambda dataset: dataset["summaries"][0]["event_count"] >= 2,
    )

    assert minimal_dataset["events"] == [
        {"id": 1, "sequence": 0},
        {"id": 2, "sequence": 1},
    ]
    assert minimal_dataset["summaries"] == [{"id": 1, "event_count": 2}]
