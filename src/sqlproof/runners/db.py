from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlproof.client import InMemorySqlProofClient


class DBManager:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    @contextmanager
    def acquire(self, *, persistent: bool = False) -> Generator[InMemorySqlProofClient]:
        del persistent
        yield InMemorySqlProofClient({})

    def stop(self) -> None:
        self.started = False
