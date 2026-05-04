from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofUsageError

if TYPE_CHECKING:
    from sqlproof.client import SqlProofClient

ExternalSeed = Callable[["SqlProofClient"], None] | Callable[["SqlProofClient", int], None]
SizeSpec = int | SearchStrategy[int]


@dataclass(frozen=True, slots=True)
class ExternalTableSpec:
    primary_key: str
    sample: Callable[[SqlProofClient], Sequence[object]]
    seed: ExternalSeed | None = None
    seed_count: SizeSpec | None = None


@dataclass(frozen=True, slots=True)
class SqlProofConfig:
    connection_string: str | None = None
    schema: str = "public"
    schema_file: str | Path | None = None
    image: str = "postgres:16"
    reuse_container: bool = True
    transaction_per_run: bool = True
    seed: Callable[[SqlProofClient], None] | None = None
    external_tables: Mapping[str, ExternalTableSpec] | None = None

    def __post_init__(self) -> None:
        sources = [self.connection_string is not None, self.schema_file is not None]
        if sum(sources) != 1:
            msg = "Exactly one of connection_string or schema_file must be provided."
            raise SqlProofUsageError(msg)
