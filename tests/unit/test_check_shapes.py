"""Common CHECK shapes are honored by both generation paths (#113).

Every shape is written twice: as a user writes it in a schema file
(what `from_schema_file` sees after pglast's deparse) and as Postgres
renders it through `pg_get_constraintdef` (what
`from_connection_string` sees -- extra parentheses, `::type` casts,
`BETWEEN` rewritten to `>= AND <=`, `IN` rewritten to `= ANY
(ARRAY[...])`). Each rendering is fed to the Hypothesis path
(`refine_for_checks`) and the bulk path (`narrow_spec_for_checks` +
`sampler_for_spec`), and every drawn value is checked against a plain
Python oracle for the constraint.
"""

from __future__ import annotations

import random
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof.generators.bulk import bulk_table_rows, sampler_for_spec
from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import (
    UnhonoredCheckWarning,
    refine_for_checks,
    unhonored_checks,
)
from sqlproof.generators.narrowing import narrow_spec_for_checks
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.generators.typespec import spec_for_type
from sqlproof.schema.checks import analyze_check, parse_check_expression
from sqlproof.schema.model import CheckConstraint, Column, ParsedCheck, PgType, Table
from sqlproof.schema.parse_sql import parse_schema_sql


@dataclass(frozen=True)
class Shape:
    id: str
    column: str
    type_name: str
    modifiers: tuple[int, ...]
    schema_file: str
    introspected: str
    holds: Callable[[Any], bool]


SHAPES = (
    Shape(
        "char_length_between",
        "task",
        "text",
        (),
        "char_length(task) BETWEEN 1 AND 200",
        "CHECK (((char_length(task) >= 1) AND (char_length(task) <= 200)))",
        lambda v: 1 <= len(v) <= 200,
    ),
    Shape(
        "length_strict_bounds_and",
        "code",
        "varchar",
        (50,),
        "length(code) > 2 AND length(code) < 6",
        "CHECK (((length((code)::text) > 2) AND (length((code)::text) < 6)))",
        lambda v: 2 < len(v) < 6,
    ),
    Shape(
        "char_length_ge",
        "title",
        "text",
        (),
        "char_length(title) >= 3",
        "CHECK ((char_length(title) >= 3))",
        lambda v: len(v) >= 3,
    ),
    Shape(
        "numeric_ge_zero",
        "total",
        "numeric",
        (10, 2),
        "total >= 0",
        "CHECK ((total >= (0)::numeric))",
        lambda v: v >= 0,
    ),
    Shape(
        "integer_gt_zero",
        "quantity",
        "integer",
        (),
        "quantity > 0",
        "CHECK ((quantity > 0))",
        lambda v: v > 0,
    ),
    Shape(
        "integer_between",
        "rating",
        "smallint",
        (),
        "rating BETWEEN 1 AND 5",
        "CHECK (((rating >= 1) AND (rating <= 5)))",
        lambda v: 1 <= v <= 5,
    ),
    Shape(
        "reversed_comparison",
        "pct",
        "integer",
        (),
        "0 <= pct AND pct <= 100",
        "CHECK (((0 <= pct) AND (pct <= 100)))",
        lambda v: 0 <= v <= 100,
    ),
    Shape(
        "negative_literal",
        "delta",
        "integer",
        (),
        "delta >= -10 AND delta < 10",
        "CHECK (((delta >= '-10'::integer) AND (delta < 10)))",
        lambda v: -10 <= v < 10,
    ),
    Shape(
        "double_precision_range",
        "ratio",
        "double precision",
        (),
        "ratio >= 0 AND ratio < 1",
        "CHECK (((ratio >= (0)::double precision) AND (ratio < (1)::double precision)))",
        lambda v: 0 <= v < 1,
    ),
    Shape(
        "real_gt",
        "score",
        "real",
        (),
        "score > 0.5",
        "CHECK ((score > (0.5)::double precision))",
        lambda v: v > 0.5,
    ),
    Shape(
        "text_in_list",
        "status",
        "text",
        (),
        "status IN ('todo', 'doing', 'done')",
        "CHECK ((status = ANY (ARRAY['todo'::text, 'doing'::text, 'done'::text])))",
        lambda v: v in {"todo", "doing", "done"},
    ),
    Shape(
        "varchar_in_list",
        "state",
        "varchar",
        (10,),
        "state IN ('open', 'closed')",
        "CHECK (((state)::text = ANY ((ARRAY['open'::character varying, "
        "'closed'::character varying])::text[])))",
        lambda v: v in {"open", "closed"},
    ),
    Shape(
        "integer_vs_decimal_literal",
        "qty",
        "integer",
        (),
        "qty > 0.5",
        "CHECK (((qty)::numeric > 0.5))",
        lambda v: v >= 1,
    ),
    Shape(
        "integer_in_list",
        "priority",
        "integer",
        (),
        "priority IN (1, 2, 3)",
        "CHECK ((priority = ANY (ARRAY[1, 2, 3])))",
        lambda v: v in {1, 2, 3},
    ),
    Shape(
        "not_empty_string",
        "name",
        "text",
        (),
        "name <> ''",
        "CHECK ((name <> ''::text))",
        lambda v: v != "",
    ),
    Shape(
        "not_in_list",
        "kind",
        "integer",
        (),
        "kind NOT IN (0, 1) AND kind >= 0 AND kind <= 3",
        "CHECK (((kind <> ALL (ARRAY[0, 1])) AND (kind >= 0) AND (kind <= 3)))",
        lambda v: v in {2, 3},
    ),
    Shape(
        "or_is_null",
        "age",
        "integer",
        (),
        "age >= 18 OR age IS NULL",
        "CHECK (((age >= 18) OR (age IS NULL)))",
        lambda v: v >= 18,
    ),
    Shape(
        "in_list_intersected_with_range",
        "level",
        "integer",
        (),
        "level IN (0, 5, 10) AND level > 0",
        "CHECK (((level = ANY (ARRAY[0, 5, 10])) AND (level > 0)))",
        lambda v: v in {5, 10},
    ),
)

RENDERINGS = ("schema_file", "introspected")

SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _column(shape: Shape, *, nullable: bool = False) -> Column:
    return Column(
        name=shape.column,
        type=PgType(kind="scalar", name=shape.type_name, modifiers=shape.modifiers),
        nullable=nullable,
        default=None,
        is_generated=False,
    )


def _expression(shape: Shape, rendering: str) -> str:
    return shape.schema_file if rendering == "schema_file" else shape.introspected


@pytest.mark.parametrize("rendering", RENDERINGS)
@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_check_shape_is_fully_recognized(shape: Shape, rendering: str) -> None:
    analysis = analyze_check(_expression(shape, rendering))
    assert analysis.complete
    assert analysis.atoms
    assert {atom.column for atom in analysis.atoms} == {shape.column}


@pytest.mark.parametrize("rendering", RENDERINGS)
@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_hypothesis_path_honors_check_shape(shape: Shape, rendering: str) -> None:
    column = _column(shape)
    checks = (CheckConstraint(_expression(shape, rendering)),)
    refined = refine_for_checks(column, strategy_for_column(column), checks)

    @SETTINGS
    @given(value=refined)
    def run(value: Any) -> None:
        assert shape.holds(value), value

    run()


@pytest.mark.parametrize("rendering", RENDERINGS)
@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_bulk_path_honors_check_shape(shape: Shape, rendering: str) -> None:
    column = _column(shape)
    checks = (CheckConstraint(_expression(shape, rendering)),)
    spec = narrow_spec_for_checks(spec_for_type(column.type), column, checks)
    sample = sampler_for_spec(spec, random.Random(113))
    for _ in range(2_000):
        value = sample()
        assert shape.holds(value), value


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.id)
def test_nullable_column_still_admits_null_alongside_check(shape: Shape) -> None:
    column = _column(shape, nullable=True)
    checks = (CheckConstraint(shape.introspected),)
    refined = refine_for_checks(column, strategy_for_column(column), checks)

    @SETTINGS
    @given(value=refined)
    def run(value: Any) -> None:
        assert value is None or shape.holds(value), value

    run()


def test_issue_113_todos_task_is_never_empty_from_schema_file() -> None:
    schema = parse_schema_sql(
        """
        create table public.todos (
          id bigint generated by default as identity primary key,
          task text not null check (char_length(task) between 1 and 200),
          is_complete boolean not null default false
        );
        """
    )
    table = schema.table("todos")
    (check,) = table.check_constraints
    assert check.parsed == ParsedCheck(
        kind="compound",
        column="task",
        payload=(
            ParsedCheck(kind="length", column="task", payload=(">=", 1)),
            ParsedCheck(kind="length", column="task", payload=("<=", 200)),
        ),
    )

    @SETTINGS
    @given(rows=table_rows_strategy(table, count=3))
    def run(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            assert 1 <= len(row["task"]) <= 200

    run()
    for row in bulk_table_rows(table, count=500, rng=random.Random(1), parent_counts={}):
        assert 1 <= len(row["task"]) <= 200


def test_parsed_is_populated_for_single_atom_and_none_for_unreadable() -> None:
    assert parse_check_expression("CHECK ((quantity > 0))") == ParsedCheck(
        kind="range", column="quantity", payload=(">", Decimal(0))
    )
    assert parse_check_expression("name ~ '^[a-z]+$'") is None


def test_partially_readable_conjunction_keeps_the_readable_part() -> None:
    analysis = analyze_check("CHECK (((qty > 0) AND (starts_at < ends_at)))")
    assert analysis.atoms == (ParsedCheck(kind="range", column="qty", payload=(">", Decimal(0))),)
    assert analysis.unrecognized == (frozenset({"starts_at", "ends_at"}),)


def test_fractional_length_bound_is_not_truncated() -> None:
    # `length(x) >= 2.5` means length >= 3; truncating to 2 would
    # admit two-character values Postgres rejects.
    assert not analyze_check("length(x) >= 2.5").atoms


def test_reserved_word_column_name_is_still_recognized() -> None:
    analysis = analyze_check("offset >= 0")
    assert analysis.complete
    assert analysis.atoms == (
        ParsedCheck(kind="range", column="offset", payload=(">=", Decimal(0))),
    )


def test_contradictory_checks_are_unsatisfiable_not_silently_violated() -> None:
    from hypothesis.errors import Unsatisfiable

    column = Column("n", PgType("scalar", "integer"), False, None, False)
    checks = (CheckConstraint("n > 10"), CheckConstraint("n < 5"))
    refined = refine_for_checks(column, strategy_for_column(column), checks)
    with pytest.raises(Unsatisfiable):
        refined.example()


def _table(*columns: Column, checks: tuple[str, ...]) -> Table:
    return Table(
        name="t113",
        schema="public",
        columns=columns,
        primary_key=(),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=tuple(CheckConstraint(c) for c in checks),
    )


def test_unhonored_check_on_a_generated_column_warns() -> None:
    table = _table(
        Column("email", PgType("scalar", "text"), False, None, False),
        checks=("CHECK ((email ~~ '%@%'::text))",),
    )
    with pytest.warns(UnhonoredCheckWarning, match="email"):
        table_rows_strategy(table, count=1)
    with pytest.warns(UnhonoredCheckWarning, match="email"):
        list(bulk_table_rows(table, count=1, rng=random.Random(0), parent_counts={}))


def test_unhonored_check_on_a_defaulted_column_does_not_warn() -> None:
    table = _table(
        Column("created_at", PgType("scalar", "timestamptz"), False, "now()", False),
        Column("qty", PgType("scalar", "integer"), False, None, False),
        checks=("CHECK ((created_at <= now()))", "CHECK ((qty > 0))"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnhonoredCheckWarning)
        table_rows_strategy(table, count=1)
        list(bulk_table_rows(table, count=1, rng=random.Random(0), parent_counts={}))


def test_unhonored_checks_reports_only_expressions_touching_generated_columns() -> None:
    checks = (
        CheckConstraint("CHECK ((a ~ '^x'::text))"),
        CheckConstraint("CHECK ((b ~ '^y'::text))"),
        CheckConstraint("CHECK ((a > 0))"),
    )
    assert unhonored_checks(checks, ["a"]) == ["CHECK ((a ~ '^x'::text))"]


def test_text_not_equal_empty_is_expressed_as_min_size() -> None:
    column = Column("name", PgType("scalar", "text"), False, None, False)
    spec = narrow_spec_for_checks(
        spec_for_type(column.type), column, (CheckConstraint("name <> ''"),)
    )
    assert spec.min_size == 1
    assert spec.excluded_values == ()
