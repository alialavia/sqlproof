from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.generators.graph import Dataset, SizeSpec, dataset_strategy
from sqlproof.schema.model import Column, PgType, SchemaInfo, Table

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


def schemas(max_tables: int = 3, max_columns: int = 5) -> SearchStrategy[SchemaInfo]:
    del max_columns
    table_names = st.lists(
        st.sampled_from(["users", "orders", "products", "scores", "events"]),
        min_size=1,
        max_size=max_tables,
        unique=True,
    )

    def build(names: list[str]) -> SchemaInfo:
        integer = PgType("scalar", "integer")
        tables = tuple(
            Table(
                schema="public",
                name=name,
                columns=(Column("id", integer, False, None, False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            )
            for name in names
        )
        return SchemaInfo(tables=tables)

    return table_names.map(build)


def datasets_for(schema: SchemaInfo, sizes: Mapping[str, SizeSpec]) -> SearchStrategy[Dataset]:
    return dataset_strategy(schema, sizes=sizes)
