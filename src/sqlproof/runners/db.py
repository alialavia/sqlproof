from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from sqlproof.client import PsycopgSqlProofClient, SqlProofClient
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofUsageError


class DBManager:
    def __init__(self, config: SqlProofConfig) -> None:
        self.config = config
        self.started = False
        self._connection: Any | None = None

    def start(self) -> None:
        if self.config.connection_string is None:
            msg = "DBManager requires a connection_string for real Postgres execution."
            raise SqlProofUsageError(msg)
        if self.started:
            return
        self._connection = psycopg.connect(
            conninfo=self.config.connection_string,
            autocommit=False,
            row_factory=cast(Any, dict_row),
        )
        self.started = True

    @contextmanager
    def acquire(self, *, persistent: bool = False) -> Generator[SqlProofClient]:
        del persistent
        if self._connection is None:
            self.start()
        if self._connection is None:
            msg = "DBManager failed to establish a database connection."
            raise SqlProofUsageError(msg)
        yield PsycopgSqlProofClient(self._connection)

    def stop(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self.started = False
