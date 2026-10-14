"""The scenario model: what a scenario says, before it meets a schema.

A scenario is a named, seeded description of a database state. Its build
function records, per table, an ordered list of row groups -- cohorts
(`n` rows with some columns pinned), named rows (a cohort of one that
other rows can reference) and fills (rows with nothing pinned) -- or a
per-parent distribution of child rows. Everything not pinned is left to
the generator.

Rows are laid out in group order, so every group occupies a fixed,
known range of row indexes. That is what lets `compile.py` turn a
scenario into the generators' existing `sizes` and row-index overrides.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlproof.exceptions import SqlProofScenarioError

if TYPE_CHECKING:
    from hypothesis.strategies import SearchStrategy

    from sqlproof.core import SqlProof
    from sqlproof.generators.graph import Dataset
    from sqlproof.scenarios.compile import CompiledScenario
    from sqlproof.scenarios.load import LoadReport
    from sqlproof.schema.model import SchemaInfo

DEFAULT_CLOCK = datetime(2026, 1, 1, tzinfo=UTC)
"""The scenario clock: what `now()`-style defaults become when a scenario
loads, so timestamps are identical on every load."""


@dataclass(frozen=True, slots=True)
class RowRef:
    """The primary-key value of a named row, resolved at compile time.

    Usable as a pinned value (typically a foreign-key column) and as a
    `per_parent(where=...)` target."""

    name: str

    def __repr__(self) -> str:
        return f"ref({self.name!r})"


@dataclass(frozen=True, slots=True)
class Group:
    kind: Literal["cohort", "row", "fill"]
    count: int
    pinned: Mapping[str, Any] = field(default_factory=dict[str, Any])
    name: str | None = None


@dataclass(frozen=True, slots=True)
class PerParent:
    parent: str
    where: Mapping[str, Any] | RowRef | None
    count: tuple[int, int]
    default: tuple[int, int]
    column: str | None


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    groups: tuple[Group, ...]
    per_parent: PerParent | None


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    seed: int
    clock: datetime
    tables: tuple[TableSpec, ...]


def _require_int(value: object, what: str, minimum: int) -> int:
    """Validate user input at runtime: `bool` is an `int` subclass and is
    rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative" if minimum == 0 else "an"
        msg = f"{what} must be a {qualifier} int; got {value!r}."
        raise SqlProofScenarioError(msg)
    return value


def _count_range(value: int | tuple[int, int], what: str) -> tuple[int, int]:
    if isinstance(value, bool):
        msg = f"{what} must be an int or an (min, max) pair, not a bool."
        raise SqlProofScenarioError(msg)
    if isinstance(value, int):
        low = high = value
    else:
        low, high = value
    if low < 0 or high < low:
        msg = f"{what} must satisfy 0 <= min <= max; got {value!r}."
        raise SqlProofScenarioError(msg)
    return low, high


class TableBuilder:
    """Row groups for one table, in the order they are declared."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._groups: list[Group] = []
        self._per_parent: PerParent | None = None

    def cohort(self, n: int, **pinned: Any) -> TableBuilder:
        """`n` rows whose listed columns take the given values; every other
        column is generated."""
        _require_int(n, f"cohort size for {self._name}", 1)
        self._groups.append(Group("cohort", n, dict(pinned)))
        return self

    def fill(self, n: int) -> TableBuilder:
        """`n` rows with nothing pinned."""
        _require_int(n, f"fill size for {self._name}", 0)
        if n:
            self._groups.append(Group("fill", n))
        return self

    def per_parent(
        self,
        parent: str,
        *,
        where: Mapping[str, Any] | RowRef | None = None,
        count: int | tuple[int, int],
        default: int | tuple[int, int] = 0,
        column: str | None = None,
    ) -> TableBuilder:
        """Size this table by its parents: each `parent` row matching
        `where` gets between `count[0]` and `count[1]` children (drawn from
        the scenario seed), every other parent `default` children.

        `where` is a mapping of columns the parent's cohorts pin, a named
        row, or None for every parent. `column` names the foreign-key
        column when this table has more than one into `parent`."""
        if self._per_parent is not None:
            msg = f"{self._name} already has a per_parent distribution; only one is supported."
            raise SqlProofScenarioError(msg)
        self._per_parent = PerParent(
            parent=parent,
            where=dict(where) if isinstance(where, Mapping) else where,
            count=_count_range(count, f"per_parent count for {self._name}"),
            default=_count_range(default, f"per_parent default for {self._name}"),
            column=column,
        )
        return self

    def _add_row(self, name: str, pinned: Mapping[str, Any]) -> None:
        self._groups.append(Group("row", 1, dict(pinned), name))

    def _spec(self) -> TableSpec:
        return TableSpec(self._name, tuple(self._groups), self._per_parent)


class ScenarioBuilder:
    """What a scenario's build function receives."""

    def __init__(self) -> None:
        self._tables: dict[str, TableBuilder] = {}
        self._row_names: set[str] = set()

    def table(self, name: str) -> TableBuilder:
        """The builder for table `name` (`"customers"` or
        `"billing.customers"`); calling it again returns the same builder."""
        if name not in self._tables:
            self._tables[name] = TableBuilder(name)
        return self._tables[name]

    def row(self, table: str, *, name: str, **pinned: Any) -> RowRef:
        """One row other rows can reference by `name`. Returns its
        reference."""
        return self._named_row(table, name, pinned)

    def _named_row(self, table: str, name: str, pinned: Mapping[str, Any]) -> RowRef:
        """`row()` without the keyword clash: the serialized form uses this,
        so a column called `name` can be pinned on a named row there."""
        if not isinstance(name, str) or not name:  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "row names must be non-empty strings."
            raise SqlProofScenarioError(msg)
        if name in self._row_names:
            msg = f"row name {name!r} is used twice; row names must be unique in a scenario."
            raise SqlProofScenarioError(msg)
        self._row_names.add(name)
        self.table(table)._add_row(name, pinned)  # pyright: ignore[reportPrivateUsage]
        return RowRef(name)

    def ref(self, name: str) -> RowRef:
        """A reference to a named row, declared before or after this call."""
        return RowRef(name)

    def _spec(self, name: str, seed: int, clock: datetime) -> ScenarioSpec:
        tables = tuple(
            builder._spec()  # pyright: ignore[reportPrivateUsage]
            for builder in self._tables.values()
        )
        return ScenarioSpec(name=name, seed=seed, clock=clock, tables=tables)


class Scenario:
    """A named, seeded database state. Create one with `@scenario(seed=...)`."""

    def __init__(
        self,
        build: Callable[[ScenarioBuilder], None],
        *,
        seed: int,
        name: str | None = None,
        clock: datetime | None = None,
    ) -> None:
        _require_int(seed, "scenario seed", -(2**63))
        self.name = name or build.__name__
        self.seed = seed
        clock = clock or DEFAULT_CLOCK
        # A clock without a timezone is UTC, not the machine's local time:
        # the same scenario must load the same rows on every machine.
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=UTC)
        self.clock = clock.astimezone(UTC)
        self.source: str | None = None
        self._build = build
        self._spec_cache: ScenarioSpec | None = None

    def __repr__(self) -> str:
        return f"Scenario({self.name!r}, seed={self.seed})"

    @property
    def spec(self) -> ScenarioSpec:
        if self._spec_cache is None:
            builder = ScenarioBuilder()
            self._build(builder)
            self._spec_cache = builder._spec(  # pyright: ignore[reportPrivateUsage]
                self.name, self.seed, self.clock
            )
        return self._spec_cache

    @property
    def fingerprint(self) -> str:
        """Hash of the normalized scenario. Callables pinned as values are
        identified by their qualified name only, so changing a callable's
        body does not change the fingerprint."""
        canonical = json.dumps(_normalize(self.spec), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """The serializable form (see `sqlproof.scenarios.serialize`). Raises
        for a scenario that pins a callable, since code cannot be stored."""
        return _normalize(self.spec, strict=True)

    def strategy(self, proof: SqlProof | SchemaInfo) -> SearchStrategy[Dataset]:
        """A Hypothesis strategy of datasets that contain this scenario.

        Every drawn dataset has the scenario's row counts, and every pinned
        value -- cohorts, named rows, references, per-parent children --
        in every draw; only the unpinned values vary, so a failing property
        shrinks those and nothing the scenario fixed. Use it to say "for
        every dataset shaped like this scenario":

            @given(dataset=tiered_customers.strategy(proof))
            def test_tier_one_never_pays_more(dataset):
                with proof.client_for_dataset(dataset) as db:
                    ...

        Unlike `load`, there is no byte-identity promise: Hypothesis chooses
        the unpinned values. A scenario that seeds `auth.users` is refused,
        because a dataset cannot carry rows for a table outside the schema;
        load it with `load` instead."""
        from sqlproof.generators.graph import dataset_strategy
        from sqlproof.schema.model import SchemaInfo

        schema = proof if isinstance(proof, SchemaInfo) else proof.schema_info
        compiled = self.compile(schema)
        if compiled.auth_users:
            msg = (
                f"scenario {self.name!r} seeds auth.users, which a Hypothesis "
                "dataset cannot carry. Load it with Scenario.load instead, or drop "
                "the auth.users rows for property tests."
            )
            raise SqlProofScenarioError(msg)
        return dataset_strategy(schema, sizes=compiled.sizes, columns=compiled.columns)

    def scaled(self, factor: int, *, scale_cohorts: bool = False) -> Scenario:
        """This scenario at about `factor` times the size, for a scale sweep.

        Fills grow by `factor`; cohorts too when `scale_cohorts` is set.
        Named rows never do: they are identities, referenced by name.
        Per-parent children grow so totals stay linear in `factor`: parents
        that stay fixed (a named row, or cohorts unless `scale_cohorts`)
        get `factor` times the children each, while parents that multiply
        keep their per-parent counts, since there are already `factor`
        times as many of them. The seed and clock stay the same."""
        _require_int(factor, "scale factor", 1)
        spec = self.spec

        def build(s: ScenarioBuilder) -> None:
            for table in spec.tables:
                builder = s.table(table.name)
                for group in table.groups:
                    if group.kind == "row":
                        assert group.name is not None
                        s._named_row(table.name, group.name, group.pinned)  # pyright: ignore[reportPrivateUsage]
                    elif group.kind == "fill":
                        builder.fill(group.count * factor)
                    else:
                        count = group.count * factor if scale_cohorts else group.count
                        builder.cohort(count, **group.pinned)
                if table.per_parent is not None:
                    pp = table.per_parent
                    # `where=None` matches every parent, fills included, so
                    # the matched parents multiply; a named row never does,
                    # and cohorts only with scale_cohorts. `default` covers
                    # the unmatched parents, which include the fills.
                    fixed = isinstance(pp.where, RowRef) or (
                        pp.where is not None and not scale_cohorts
                    )
                    grow = factor if fixed else 1
                    builder.per_parent(
                        pp.parent,
                        where=pp.where,
                        count=(pp.count[0] * grow, pp.count[1] * grow),
                        default=pp.default,
                        column=pp.column,
                    )

        return Scenario(build, seed=self.seed, name=self.name, clock=self.clock)

    def minimal(self) -> Scenario:
        """At most one row per group, for a scale sweep's calibration round:
        the same shape, so the same tables and references exist, with as
        little data as the scenario allows."""
        spec = self.spec

        def build(s: ScenarioBuilder) -> None:
            for table in spec.tables:
                builder = s.table(table.name)
                for group in table.groups:
                    if group.kind == "row":
                        assert group.name is not None
                        s._named_row(table.name, group.name, group.pinned)  # pyright: ignore[reportPrivateUsage]
                    elif group.kind == "fill":
                        builder.fill(min(group.count, 1))
                    else:
                        builder.cohort(1, **group.pinned)
                if table.per_parent is not None:
                    pp = table.per_parent
                    builder.per_parent(
                        pp.parent,
                        where=pp.where,
                        count=(min(pp.count[0], 1), min(pp.count[1], 1)),
                        default=0,
                        column=pp.column,
                    )

        return Scenario(build, seed=self.seed, name=self.name, clock=self.clock)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scenario:
        """The scenario a serialized form describes; the inverse of
        `to_dict`. Raises `SqlProofScenarioError` naming the offending
        path when `data` is not a valid version 1 scenario."""
        from sqlproof.scenarios.serialize import scenario_from_dict

        return scenario_from_dict(data)

    def compile(self, schema: SchemaInfo) -> CompiledScenario:
        from sqlproof.scenarios.compile import compile_scenario

        return compile_scenario(self, schema)

    def load(
        self, dsn: str, *, schema: str = "public", truncate_existing: bool = False
    ) -> LoadReport:
        """Load this scenario into the database at `dsn`. See
        `sqlproof.scenarios.load.load_scenario`."""
        from sqlproof.scenarios.load import load_scenario

        return load_scenario(self, dsn, schema=schema, truncate_existing=truncate_existing)


def scenario(
    *, seed: int, name: str | None = None, clock: datetime | None = None
) -> Callable[[Callable[[ScenarioBuilder], None]], Scenario]:
    """Declare a scenario::

    @scenario(seed=42)
    def tiered_customers(s):
        s.table("customers").cohort(2, tier=1).cohort(3, tier=2).fill(20)
    """

    def decorate(build: Callable[[ScenarioBuilder], None]) -> Scenario:
        return Scenario(build, seed=seed, name=name, clock=clock)

    return decorate


def _normalize(spec: ScenarioSpec, *, strict: bool = False) -> dict[str, Any]:
    def value(v: Any, where: str) -> Any:
        if isinstance(v, RowRef):
            return {"ref": v.name}
        if callable(v):
            if strict:
                msg = (
                    f"{where} is pinned to a callable, which cannot be serialized. "
                    "Pin a literal or a reference instead."
                )
                raise SqlProofScenarioError(msg)
            module = getattr(v, "__module__", "?")
            qualname = getattr(v, "__qualname__", type(v).__name__)
            return {"callable": f"{module}.{qualname}"}
        if isinstance(v, str | int | float | bool) or v is None:
            return v
        # Written as Postgres reads them, so a Python scenario and its
        # export have the same fingerprint and load the same rows.
        if isinstance(v, datetime | date | time):
            return v.isoformat()
        if isinstance(v, Decimal | UUID):
            return str(v)
        if strict:
            msg = (
                f"{where} is pinned to a {type(v).__name__}, which has no serialized "
                "form. Pin a string Postgres can read instead."
            )
            raise SqlProofScenarioError(msg)
        return {"literal": repr(v)}

    def group(g: Group, table: str) -> dict[str, Any]:
        # A named row is written by name: `{"row": "acme"}`.
        out: dict[str, Any] = {g.kind: g.name if g.kind == "row" else g.count}
        if g.pinned:
            out["set"] = {k: value(v, f"{table}.{k}") for k, v in sorted(g.pinned.items())}
        return out

    tables: dict[str, Any] = {}
    for table in spec.tables:
        entries: list[dict[str, Any]] = [group(g, table.name) for g in table.groups]
        if table.per_parent is not None:
            pp = table.per_parent
            where: Any
            if isinstance(pp.where, RowRef):
                where = {"ref": pp.where.name}
            elif pp.where is None:
                where = None
            else:
                where = {k: value(v, f"{pp.parent}.{k}") for k, v in sorted(pp.where.items())}
            per_parent: dict[str, Any] = {
                "parent": pp.parent,
                "count": list(pp.count),
                "default": list(pp.default),
            }
            # Unset options are left out, so exported files stay short.
            if where is not None:
                per_parent["where"] = where
            if pp.column is not None:
                per_parent["column"] = pp.column
            entries.append({"per_parent": per_parent})
        tables[table.name] = entries
    return {
        "version": 1,
        "name": spec.name,
        "seed": spec.seed,
        "clock": spec.clock.isoformat(),
        "tables": tables,
    }
