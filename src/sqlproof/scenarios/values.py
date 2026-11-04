"""Value generators: a pinned value that differs per row.

A literal pinned on a group gives every row the same value. A generator
gives each row its own, as a pure function of the scenario, its seed, the
column and the row, so a scenario still loads the same bytes every time:

- `format("https://acme.com/blog/{n}")`: text with `{n}` replaced by the
  row's number in its table (1, 2, 3, ...), so it is unique.
- `one_of(a, b, c)`: one of the values.
- `some_of([a, b, c], count=(lo, hi))`: an array of `lo` to `hi` distinct
  values.
- `pick("pages.url", count=(lo, hi))`: values another column of the
  scenario holds -- an array of `lo` to `hi` of them, or one value
  without `count`. That is how rows carry a relationship the schema does
  not declare, such as a response citing a page's URL.

They are dataclasses rather than callables, so a scenario that uses them
still serializes (`{format: ...}`, `{one_of: [...]}`, ...) and keeps a
fingerprint that changes when they do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlproof.exceptions import SqlProofScenarioError

N = "{n}"
KEYS = frozenset({"format", "one_of", "some_of", "pick"})
"""The keys a serialized value generator is written with."""


@dataclass(frozen=True, slots=True)
class Format:
    pattern: str


@dataclass(frozen=True, slots=True)
class OneOf:
    options: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class SomeOf:
    options: tuple[Any, ...]
    count: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Pick:
    table: str
    column: str
    count: tuple[int, int] | None

    @property
    def source(self) -> str:
        return f"{self.table}.{self.column}"


Generator = Format | OneOf | SomeOf | Pick


def is_generator(value: Any) -> bool:
    return isinstance(value, Format | OneOf | SomeOf | Pick)


def has_generator(value: Any) -> bool:
    """A generator, or an array literal with one among its elements
    (`names: [format("Rival {n}")]`)."""
    if is_generator(value):
        return True
    if isinstance(value, list | tuple):
        items = cast("Sequence[Any]", value)
        return any(is_generator(item) for item in items)
    return False


def format_(pattern: str) -> Format:
    if not isinstance(pattern, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"format must be a string with {N} in it; got {pattern!r}."
        raise SqlProofScenarioError(msg)
    return Format(pattern)


def one_of(*options: Any) -> OneOf:
    if not options:
        msg = "one_of needs at least one value."
        raise SqlProofScenarioError(msg)
    return OneOf(tuple(options))


def some_of(options: Sequence[Any], *, count: int | tuple[int, int]) -> SomeOf:
    if isinstance(options, str) or not options:
        msg = f"some_of needs a non-empty list of values; got {options!r}."
        raise SqlProofScenarioError(msg)
    return SomeOf(tuple(options), count_range(count, "some_of count"))


def pick(source: str, *, count: int | tuple[int, int] | None = None) -> Pick:
    if not isinstance(source, str) or source.count(".") not in (1, 2):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"pick names a column as table.column (or schema.table.column); got {source!r}."
        raise SqlProofScenarioError(msg)
    table, column = source.rsplit(".", 1)
    if not table or not column:
        msg = f"pick names a column as table.column (or schema.table.column); got {source!r}."
        raise SqlProofScenarioError(msg)
    return Pick(table, column, None if count is None else count_range(count, "pick count"))


def count_range(value: Any, what: str) -> tuple[int, int]:
    if isinstance(value, bool):
        msg = f"{what} must be an int or a [min, max] pair; got {value!r}."
        raise SqlProofScenarioError(msg)
    if isinstance(value, int):
        low = high = value
    elif isinstance(value, list | tuple) and len(cast("Sequence[Any]", value)) == 2:
        low, high = cast("Sequence[Any]", value)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (low, high)):
            msg = f"{what} must be an int or a [min, max] pair; got {value!r}."
            raise SqlProofScenarioError(msg)
    else:
        msg = f"{what} must be an int or a [min, max] pair; got {value!r}."
        raise SqlProofScenarioError(msg)
    if low < 0 or high < low:
        msg = f"{what} must satisfy 0 <= min <= max; got {value!r}."
        raise SqlProofScenarioError(msg)
    return low, high


def to_dict(value: Generator, plain: Any) -> dict[str, Any]:
    """The serialized form; `plain` normalizes option values."""
    if isinstance(value, Format):
        return {"format": value.pattern}
    if isinstance(value, OneOf):
        return {"one_of": [plain(option) for option in value.options]}
    if isinstance(value, SomeOf):
        return {"some_of": [plain(option) for option in value.options], "count": list(value.count)}
    out: dict[str, Any] = {"pick": value.source}
    if value.count is not None:
        out["count"] = list(value.count)
    return out


def looks_like_generator(value: Mapping[str, Any]) -> bool:
    """Would this mapping, written in a scenario file, read as a generator?

    A json literal of that shape is written wrapped (`{json: ...}`) so it
    reads back as data."""
    return bool(set(value) & KEYS) or set(value) == {"json"}
