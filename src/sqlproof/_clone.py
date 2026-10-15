"""Cloning a database from a template, for work that must not touch it.

Shared by mutation testing (a clone per mutant) and scale analysis (a
sweep in a clone). `CREATE DATABASE ... TEMPLATE` needs the template to
have no other sessions; see `create` for how pooler sessions are handled.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from sqlproof import _database as database
from sqlproof import _dsn as conninfo
from sqlproof._dsn import quote_identifier

# SQLSTATE 55006: "source database ... is being accessed by other users".
_OBJECT_IN_USE = "55006"

# Postgres truncates identifiers longer than NAMEDATALEN - 1 bytes.
_MAX_IDENTIFIER_BYTES = 63


class TemplateCloner:
    """Creates and drops clones of the `database_url` database.

    Clone creation and drops are serialized behind one lock: the template
    may have no other sessions while it is copied. Errors are raised as
    `error`, so each caller reports them in its own exception family."""

    def __init__(
        self,
        *,
        database_url: str,
        run_id: str,
        error: type[Exception],
        maintenance_db: str = "postgres",
        release_template_sessions: bool = False,
    ) -> None:
        self.database_url = database_url
        self.run_id = run_id
        self.error = error
        self.maintenance_db = maintenance_db
        self.release_template_sessions = release_template_sessions
        self._clone_lock = threading.Lock()

    def create(self, clone_name: str) -> str:
        template = self._dbname(self.database_url)
        with self._clone_lock, database.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                f"DROP DATABASE IF EXISTS {quote_identifier(clone_name)} WITH (FORCE)"
            )
            create = (
                f"CREATE DATABASE {quote_identifier(clone_name)} "
                f"TEMPLATE {quote_identifier(template)}"
            )
            try:
                connection.execute(create)
            except database.DatabaseError as exc:
                if database.sqlstate(exc) != _OBJECT_IN_USE:
                    raise
                self._clone_past_template_sessions(connection, template, create, exc)
        return self._dsn_for(clone_name)

    def _clone_past_template_sessions(
        self,
        connection: database.Connection,
        template: str,
        create: str,
        error: BaseException,
    ) -> None:
        """Handle ``CREATE DATABASE ... TEMPLATE`` refused for other sessions.

        A connection pooler (Supavisor, PgBouncer) keeps idle server
        sessions open on every database a client used, including the
        template, and Supavisor reopens one as soon as it is terminated.
        With ``release_template_sessions`` the cloner closes the template
        to new connections (``ALLOW_CONNECTIONS false``), terminates the
        *idle* sessions of its own role there, clones, and always reopens
        the template. Otherwise, or when any blocking session is active or
        belongs to another role, it raises an error naming the sessions.

        Background workers are not client sessions and are never terminated.
        Some visit every database briefly (e.g. TimescaleDB's per-database
        scheduler on Supabase images), so they are left to ``CREATE
        DATABASE``'s own wait for other sessions to exit; one still there
        after it is named in the error like any other session.
        """
        quoted = quote_identifier(template)
        sessions = self._template_sessions(connection, template)
        clients = [session for session in sessions if session[4]]
        releasable = [pid for pid, _app, state, own, _client in clients if own and state == "idle"]
        if not (self.release_template_sessions and len(releasable) == len(clients)):
            raise self._template_busy_error(template, sessions) from error
        connection.execute(f"ALTER DATABASE {quoted} ALLOW_CONNECTIONS false")
        try:
            for pid in releasable:
                connection.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,))
            remaining = [s for s in self._template_sessions(connection, template) if s[4]]
            if remaining:
                raise self._template_busy_error(template, remaining) from error
            try:
                connection.execute(create)
            except database.DatabaseError as exc:
                if database.sqlstate(exc) != _OBJECT_IN_USE:
                    raise
                still = self._template_sessions(connection, template)
                raise self._template_busy_error(template, still) from exc
        finally:
            connection.execute(f"ALTER DATABASE {quoted} ALLOW_CONNECTIONS true")

    @staticmethod
    def _template_sessions(
        connection: database.Connection, template: str
    ) -> list[tuple[int, str, str, bool, bool]]:
        # (pid, application_name, state, same role as us, client session)
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT pid, coalesce(application_name, ''), coalesce(state, ''), "
                "usename = session_user, backend_type = 'client backend' "
                "FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (template,),
            ).fetchall()
        ]

    def _template_busy_error(
        self, template: str, sessions: Sequence[tuple[int, str, str, bool, bool]]
    ) -> Exception:
        apps = sorted({app or "<unnamed>" for _pid, app, _state, _own, _client in sessions})
        hint = (
            "they are not all idle sessions of this role, so they were left alone"
            if self.release_template_sessions
            else "if a connection pooler holds them (e.g. Supavisor), pass "
            "release_template_sessions=True to terminate idle sessions of this "
            "role on the template, or connect to the server directly"
        )
        return self.error(
            f"cannot clone template database {template!r}: {len(sessions)} other "
            f"session(s) are connected (application_name: {', '.join(apps)}); {hint}."
        )

    def drop(self, clone_name: str) -> None:
        with self._clone_lock, database.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                f"DROP DATABASE IF EXISTS {quote_identifier(clone_name)} WITH (FORCE)"
            )

    def clone_name(self, stem: str) -> str:
        # The run-id suffix keeps concurrent runs on one cluster apart; trim
        # the stem, never the suffix, to stay within the identifier limit.
        suffix = f"_{self.run_id}".encode()
        room = _MAX_IDENTIFIER_BYTES - len(suffix)
        return stem.encode()[:room].decode(errors="ignore") + suffix.decode()

    def _dsn_for(self, dbname: str) -> str:
        # Keep credentials and transport options when switching databases.
        return conninfo.make_conninfo(self.database_url, dbname=dbname)

    def _dbname(self, dsn: str) -> str:
        dbname = conninfo.conninfo_to_dict(dsn).get("dbname")
        if not dbname:
            msg = "database_url has no dbname; specify the template database name."
            raise self.error(msg)
        return str(dbname)

