from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.generators.graph import Dataset, SizeSpec, dataset_strategy
from sqlproof.schema.model import CheckConstraint, Column, ForeignKey, PgType, SchemaInfo, Table

if TYPE_CHECKING:
    from sqlproof.client import SqlProofClient
    from sqlproof.core import SqlProof

_T = TypeVar("_T")


class SqlProofStateMachine(RuleBasedStateMachine):
    """Base class for Hypothesis stateful tests against a SqlProof database.

    Each example leases an isolated client via `proof.client_for_dataset(...)`
    so that writes from one example are rolled back before the next begins.
    Subclasses define `@rule`s and `@invariant`s as usual, and override
    `on_setup()` for per-example fixture creation. `self.db` is the live
    `SqlProofClient`.

    Run a machine with `SqlProof.run_state_machine(MyMachine, settings=...)`,
    which binds the proof and dispatches to `run_state_machine_as_test`.
    """

    initial_dataset: ClassVar[dict[str, list[dict[str, Any]]]] = {}
    _sqlproof_proof: ClassVar[SqlProof | None] = None

    db: SqlProofClient

    def __init__(self) -> None:
        if self._sqlproof_proof is None:
            msg = (
                "SqlProofStateMachine cannot be instantiated directly. "
                "Use SqlProof.run_state_machine(YourMachine) to run it."
            )
            raise SqlProofUsageError(msg)
        super().__init__()
        self._stack = ExitStack()
        self.db = self._stack.enter_context(
            self._sqlproof_proof.client_for_dataset(dict(self.initial_dataset))
        )
        self.on_setup()

    def on_setup(self) -> None:
        """Override to seed per-example fixtures. `self.db` is ready."""

    def enter(self, cm: AbstractContextManager[_T]) -> _T:
        """Enter `cm` and tie its lifetime to this example.

        Use for resources that need to live across rules within an example
        and be released between examples — JWT-claim contexts, savepoints,
        mocked clocks, etc. The context manager is closed during `teardown`
        in reverse-entry order.
        """
        return self._stack.enter_context(cm)

    def teardown(self) -> None:
        if hasattr(self, "_stack"):
            self._stack.close()


# Column types drawn for non-`id` columns. Deliberately spans the
# constructs the two generation paths (Hypothesis search vs. seeded
# bulk RNG) could disagree on: integer width, text, boolean, decimal
# precision, date, uuid and jsonb (the `Jsonb(None)` vs. jsonb scalar
# `null` distinction only has a column to fail on if jsonb is here).
_SCHEMA_COLUMN_TYPES: tuple[PgType, ...] = (
    PgType("scalar", "integer"),
    PgType("scalar", "bigint"),
    PgType("scalar", "text"),
    PgType("scalar", "boolean"),
    PgType("scalar", "numeric", modifiers=(10, 2)),
    PgType("scalar", "date"),
    PgType("scalar", "uuid"),
    PgType("scalar", "jsonb"),
)

# CHECK comparison operators drawn for the `>= 0` / `> 0` constraint on
# non-nullable integer/bigint/numeric columns. The strict `>` shape
# exercises decimal/integer bound *stepping* (see narrowing.py's
# `_narrow_bound`), which `>=` alone never touches -- a strict bound
# has to round its endpoint one unit away from the literal instead of
# using it directly.
_CHECK_OPERATORS: tuple[str, ...] = (">=", ">")


def schemas(
    max_tables: int = 3,
    max_columns: int = 5,
    *,
    with_foreign_keys: bool = True,
    with_checks: bool = True,
) -> SearchStrategy[SchemaInfo]:
    """Generate varied schemas for cross-path differential testing.

    Must produce the constructs where the two generation paths could
    disagree -- varied types, nullability, foreign keys and CHECK
    constraints. A strategy that only emits `id integer` tables cannot
    find a divergence, because it never generates anything that diverges.

    FK columns only ever reference an earlier table (by draw position),
    so the generated FK graph is always acyclic.

    A table with two distinct earlier tables to reference (position >=
    2) can, in addition to the usual single FK column, draw a *second*
    FK column referencing a different earlier table -- and then draw
    whether to make the primary key the pair of them instead of `id`.
    This is the junction-table shape the composite-primary-key mixed-
    radix assignment (`composite_key_values` in generators/bulk.py)
    exists for; both the single-FK and composite-PK shapes stay
    reachable, drawn rather than forced.
    """
    names = st.lists(
        st.sampled_from(["users", "orders", "products", "scores", "events"]),
        min_size=1,
        max_size=max_tables,
        unique=True,
    )

    @st.composite
    def build(draw: st.DrawFn) -> SchemaInfo:
        table_names = draw(names)
        tables: list[Table] = []
        for position, name in enumerate(table_names):
            # id (1) + n_columns + an optional FK column (0 or 1) must
            # never exceed max_columns. Capping n_columns at
            # max_columns - 2 (floored at 1) leaves room for both.
            n_columns = draw(st.integers(min_value=1, max_value=max(1, max_columns - 2)))
            columns = [Column("id", PgType("scalar", "bigint"), False, None, False)]
            checks: list[CheckConstraint] = []
            foreign_keys: list[ForeignKey] = []

            for i in range(n_columns):
                col_type = draw(st.sampled_from(_SCHEMA_COLUMN_TYPES))
                nullable = draw(st.booleans())
                col_name = f"c{i}"
                columns.append(Column(col_name, col_type, nullable, None, False))
                if (
                    with_checks
                    and not nullable
                    and col_type.name in {"integer", "bigint", "numeric"}
                    and draw(st.booleans())
                ):
                    operator = draw(st.sampled_from(_CHECK_OPERATORS))
                    checks.append(CheckConstraint(expression=f"{col_name} {operator} 0"))

            # Reference an earlier table so the FK graph stays acyclic.
            primary_key: tuple[str, ...] = ("id",)
            if with_foreign_keys and position > 0 and draw(st.booleans()):
                parent = table_names[draw(st.integers(0, position - 1))]
                fk_name = f"{parent}_id"
                columns.append(
                    Column(fk_name, PgType("scalar", "bigint"), False, None, False)
                )
                foreign_keys.append(
                    ForeignKey(
                        columns=(fk_name,),
                        referenced_table=parent,
                        referenced_columns=("id",),
                        on_delete="NO ACTION",
                        on_update="NO ACTION",
                    )
                )

                # A second, distinct earlier table lets this table take
                # the junction-table shape: a composite primary key over
                # both FK columns rather than the synthetic `id`. Only
                # possible with >= 2 earlier tables, and only drawn when
                # there's still column budget for one more column.
                other_parents = [p for p in table_names[:position] if p != parent]
                if other_parents and len(columns) < max_columns and draw(st.booleans()):
                    parent2 = draw(st.sampled_from(other_parents))
                    fk2_name = f"{parent2}_id"
                    columns.append(
                        Column(fk2_name, PgType("scalar", "bigint"), False, None, False)
                    )
                    foreign_keys.append(
                        ForeignKey(
                            columns=(fk2_name,),
                            referenced_table=parent2,
                            referenced_columns=("id",),
                            on_delete="NO ACTION",
                            on_update="NO ACTION",
                        )
                    )
                    if draw(st.booleans()):
                        primary_key = (fk_name, fk2_name)

            tables.append(
                Table(
                    schema="public",
                    name=name,
                    columns=tuple(columns),
                    primary_key=primary_key,
                    foreign_keys=tuple(foreign_keys),
                    unique_constraints=(),
                    check_constraints=tuple(checks),
                )
            )
        return SchemaInfo(tables=tuple(tables))

    return build()


def datasets_for(schema: SchemaInfo, sizes: Mapping[str, SizeSpec]) -> SearchStrategy[Dataset]:
    return dataset_strategy(schema, sizes=sizes)
