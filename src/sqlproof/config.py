from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlproof.exceptions import SqlProofUsageError

if TYPE_CHECKING:
    from sqlproof.client import SqlProofClient


@dataclass(frozen=True, slots=True)
class SqlProofConfig:
    connection_string: str | None = None
    schema: str = "public"
    schema_file: str | Path | None = None
    image: str = "postgres:16"
    reuse_container: bool = True
    transaction_per_run: bool = True
    seed: Callable[[SqlProofClient], None] | None = None

    def __post_init__(self) -> None:
        sources = [self.connection_string is not None, self.schema_file is not None]
        if sum(sources) != 1:
            msg = "Exactly one of connection_string or schema_file must be provided."
            raise SqlProofUsageError(msg)
