"""The serialized form of a scenario: YAML or JSON, without code.

What `Scenario.to_dict()` writes and `Scenario.from_dict()` reads, so
projects that do not write Python -- a TypeScript Supabase app, an
agent over MCP -- can describe a database state too::

    version: 1
    name: tiered_customers
    seed: 42
    tables:
      customers:
        - row: acme
          set: {tier: 1}
        - cohort: 2
          set: {tier: 1}
        - fill: 20
      invoices:
        - per_parent:
            parent: customers
            where: {tier: 1}
            count: [5, 10]

The JSON Schema for editors ships as `sqlproof/schemas/scenario-v1.json`.
The parser here does not need it: it checks the same rules itself and
names the path of anything it rejects.

Unknown top-level keys are rejected, except under `x-*`, which tools
building on scenarios may use for their own settings. They are not part
of the scenario: they do not change its fingerprint or its rows.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from importlib import resources
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scenarios.model import RowRef, Scenario, ScenarioBuilder

FORMAT_VERSION = 1
SCENARIO_FILE_SUFFIXES = (".scenario.yaml", ".scenario.yml", ".scenario.json")
"""File names `discover` loads as serialized scenarios, one per file."""

_TOP_LEVEL = {"version", "name", "seed", "clock", "tables"}
_GROUP_KINDS = ("cohort", "fill", "row", "per_parent")
_PER_PARENT_KEYS = {"parent", "where", "count", "default", "column"}


def scenario_from_dict(data: Mapping[str, Any]) -> Scenario:
    """The scenario `data` describes. See the module docstring for the shape."""
    top = _mapping(data, "$", "a scenario must be a mapping")
    unknown = sorted(k for k in top if k not in _TOP_LEVEL and not k.startswith("x-"))
    if unknown:
        _fail("$", f"unknown key(s) {', '.join(map(repr, unknown))}; extensions go under x-*")
    version = top.get("version")
    if version != FORMAT_VERSION:
        _fail("$.version", f"must be {FORMAT_VERSION}; got {version!r}")
    name = _name(top.get("name"), "$.name", "must be a non-empty string")
    seed = _int(top.get("seed"), "$.seed")
    clock = _clock(top.get("clock"))
    tables = _mapping(
        top.get("tables"), "$.tables", "must be a mapping of table name to row groups"
    )
    replays = [_table_replay(table, groups) for table, groups in tables.items()]

    def build(s: ScenarioBuilder) -> None:
        for replay in replays:
            replay(s)

    built = Scenario(build, seed=seed, name=name, clock=clock)
    # Surface builder errors (a duplicate row name, a bad count) now, at
    # parse time, rather than at the first compile.
    built.spec  # noqa: B018
    return built


def load_file(path: str | Path) -> Scenario:
    """The scenario in a `.json`, `.yaml` or `.yml` file. YAML needs the
    `yaml` extra (`pip install "sqlproof[yaml]"`)."""
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    try:
        if file.suffix == ".json":
            data = json.loads(text)
        elif file.suffix in (".yaml", ".yml"):
            data = _yaml().safe_load(text)
        else:
            msg = f"{file}: scenario files end in .json, .yaml or .yml."
            raise SqlProofScenarioError(msg)
    except (json.JSONDecodeError, _yaml_error()) as exc:
        msg = f"{file}: not valid {file.suffix.lstrip('.').upper()}: {exc}"
        raise SqlProofScenarioError(msg) from exc
    try:
        loaded = scenario_from_dict(data)
    except SqlProofScenarioError as exc:
        msg = f"{file}: {exc}"
        raise SqlProofScenarioError(msg) from exc
    loaded.source = str(file)
    return loaded


def dumps(scenario: Scenario, fmt: Literal["yaml", "json"] = "yaml") -> str:
    """`scenario` in its serialized form, as YAML or JSON text."""
    data = scenario.to_dict()
    if fmt == "json":
        return json.dumps(data, indent=2) + "\n"
    dumped: str = _yaml().safe_dump(data, sort_keys=False, allow_unicode=True)
    return dumped


def json_schema() -> dict[str, Any]:
    """The JSON Schema for version 1 scenario files."""
    text = resources.files("sqlproof").joinpath("schemas/scenario-v1.json").read_text("utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


Replay = Callable[[ScenarioBuilder], object]


def _table_replay(table: str, groups: Any) -> Replay:
    where = f"$.tables[{table!r}]"
    if not isinstance(groups, list):
        _fail(where, f"must be a list of row groups; got {_kind(groups)}")
    items = cast("list[Any]", groups)  # type: ignore[redundant-cast]
    steps = [_group_replay(table, group, f"{where}[{i}]") for i, group in enumerate(items)]

    def replay(s: ScenarioBuilder) -> None:
        s.table(table)  # declare the table even with no groups
        for step in steps:
            step(s)

    return replay


def _group_replay(table: str, raw: Any, where: str) -> Replay:
    group = _mapping(raw, where, "must be a mapping such as {cohort: 2, set: {...}}")
    kinds = [k for k in _GROUP_KINDS if k in group]
    if len(kinds) != 1:
        _fail(where, f"must have exactly one of {', '.join(_GROUP_KINDS)}; got {sorted(group)}")
    kind = kinds[0]
    allowed = {kind} if kind in ("fill", "per_parent") else {kind, "set"}
    extra = sorted(k for k in group if k not in allowed)
    if extra:
        _fail(where, f"unexpected key(s) {', '.join(map(repr, extra))} in a {kind} group")
    pinned = _pinned(group.get("set", {}), f"{where}.set")
    value = group[kind]
    if kind == "cohort":
        count = _int(value, f"{where}.cohort")
        return lambda s: s.table(table).cohort(count, **pinned)
    if kind == "fill":
        count = _int(value, f"{where}.fill")
        return lambda s: s.table(table).fill(count)
    if kind == "row":
        row = _name(value, f"{where}.row", "must be the row's name, a non-empty string")
        return lambda s: s._named_row(table, row, pinned)  # pyright: ignore[reportPrivateUsage]
    return _per_parent_replay(table, value, f"{where}.per_parent")


def _per_parent_replay(table: str, raw: Any, where: str) -> Replay:
    spec = _mapping(raw, where, "must be a mapping")
    extra = sorted(k for k in spec if k not in _PER_PARENT_KEYS)
    if extra:
        _fail(where, f"unexpected key(s) {', '.join(map(repr, extra))}")
    parent = _name(spec.get("parent"), f"{where}.parent", "must be a table name")
    if "count" not in spec:
        _fail(where, "is missing count")
    count = _count(spec["count"], f"{where}.count")
    default = _count(spec.get("default", 0), f"{where}.default")
    raw_column = spec.get("column")
    column = (
        None
        if raw_column is None
        else _name(raw_column, f"{where}.column", "must be a column name")
    )
    raw_where = spec.get("where")
    match: Mapping[str, Any] | RowRef | None
    if raw_where is None:
        match = None
    elif (ref := _ref(raw_where)) is not None:
        match = ref
    else:
        match = _pinned(raw_where, f"{where}.where")
    return lambda s: s.table(table).per_parent(
        parent, where=match, count=count, default=default, column=column
    )


def _pinned(raw: Any, where: str) -> dict[str, Any]:
    columns = _mapping(raw, where, "must be a mapping of column to value")
    return {column: _value(value, f"{where}.{column}") for column, value in columns.items()}


def _value(value: Any, where: str) -> Any:
    ref = _ref(value)
    if ref is not None:
        return ref
    if isinstance(value, Mapping | list):
        _fail(
            where,
            "must be a scalar or {ref: <row name>}; to pin a json or array column, "
            "write the value as a string",
        )
    if value is None or isinstance(value, str | int | float | bool | date | datetime | time):
        return value
    _fail(where, f"has unsupported type {type(value).__name__}")


def _ref(value: Any) -> RowRef | None:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[Any, Any]", value)  # type: ignore[redundant-cast]
        name = mapping.get("ref")
        if set(mapping) == {"ref"} and isinstance(name, str):
            return RowRef(name)
    return None


def _mapping(value: Any, where: str, problem: str) -> Mapping[str, Any]:
    """`value` as a mapping with string keys, or a failure naming `where`."""
    if not isinstance(value, Mapping):
        _fail(where, f"{problem}; got {_kind(value)}")
    mapping = cast("Mapping[Any, Any]", value)  # type: ignore[redundant-cast]
    return {str(key): item for key, item in mapping.items()}


def _name(value: Any, where: str, problem: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(where, f"{problem}; got {value!r}")
    return value


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(where, f"must be an integer; got {value!r}")
    number: int = value
    return number


def _count(value: Any, where: str) -> int | tuple[int, int]:
    if isinstance(value, list):
        pair = cast("list[Any]", value)  # type: ignore[redundant-cast]
        if len(pair) != 2:
            _fail(where, f"must be an integer or a [min, max] pair; got {value!r}")
        return (_int(pair[0], f"{where}[0]"), _int(pair[1], f"{where}[1]"))
    return _int(value, where)


def _clock(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    _fail("$.clock", f"must be an ISO 8601 timestamp such as 2026-01-01T00:00:00Z; got {value!r}")


def _kind(value: Any) -> str:
    return "nothing" if value is None else f"a {type(value).__name__}"


def _fail(where: str, problem: str) -> NoReturn:
    msg = f"{where} {problem}."
    raise SqlProofScenarioError(msg)


def _yaml() -> Any:
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:
        msg = (
            "YAML scenarios need PyYAML: pip install 'sqlproof[yaml]'. "
            "JSON scenario files (*.scenario.json) work without it."
        )
        raise SqlProofScenarioError(msg) from exc


def _yaml_error() -> type[Exception]:
    try:
        error: type[Exception] = importlib.import_module("yaml").YAMLError
    except ImportError:
        return json.JSONDecodeError
    return error
