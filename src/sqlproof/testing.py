from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hypothesis import strategies as st
from hypothesis.control import _current_build_context
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

    Two ways to seed per-example fixture data:

    * ``initial_dataset`` — a ``ClassVar[dict[str, list[dict]]]`` with
      hard-coded rows.  Good when the test needs a precise, known shape.
    * ``sizes`` — a ``ClassVar[dict[str, int | SearchStrategy[int]]]``
      that drives the schema-backed generator.  When set, Hypothesis
      generates a fresh dataset for each example and exposes it as
      ``self.dataset``.  Use ``on_setup`` to read IDs / values from
      ``self.dataset`` and force the rows into a known initial state.

    Run a machine with `SqlProof.run_state_machine(MyMachine, settings=...)`,
    which binds the proof and dispatches to `run_state_machine_as_test`.
    """

    initial_dataset: ClassVar[dict[str, list[dict[str, Any]]]] = {}
    sizes: ClassVar[dict[str, SizeSpec]] = {}
    _sqlproof_proof: ClassVar[SqlProof | None] = None

    db: SqlProofClient
    dataset: dict[str, list[dict[str, Any]]]

    def __init__(self) -> None:
        if self._sqlproof_proof is None:
            msg = (
                "SqlProofStateMachine cannot be instantiated directly. "
                "Use SqlProof.run_state_machine(YourMachine) to run it."
            )
            raise SqlProofUsageError(msg)
        super().__init__()
        self._stack = ExitStack()
        if self.sizes:
            # Draw a dataset via the current Hypothesis build context so
            # that the generated values participate in shrinking.  The
            # machine is instantiated inside a @given(st.data()) test, so
            # _current_build_context.value.data is available and accepts
            # .draw() calls exactly as it would inside a @composite strategy.
            ctx = _current_build_context.value
            if ctx is not None and ctx.data is not None:
                self.dataset = ctx.data.draw(
                    self._sqlproof_proof.dataset_strategy(sizes=self.sizes)
                )
            else:
                # Fallback for direct instantiation in non-test contexts
                # (e.g. interactive exploration); use strategy.example().
                import warnings

                from hypothesis.errors import NonInteractiveExampleWarning

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", NonInteractiveExampleWarning)
                    self.dataset = self._sqlproof_proof.dataset_strategy(
                        sizes=self.sizes
                    ).example()
        else:
            self.dataset = dict(self.initial_dataset)
        self.db = self._stack.enter_context(
            self._sqlproof_proof.client_for_dataset(self.dataset)
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
