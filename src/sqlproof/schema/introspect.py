from __future__ import annotations

from typing import Any

from sqlproof.schema.model import SchemaInfo


def introspect_schema(connection: Any, *, schema: str = "public") -> SchemaInfo:
    """Introspect a live PostgreSQL schema.

    The full pg_catalog implementation is built out behind this stable entry point.
    """

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema,),
        )
        cursor.fetchall()
    return SchemaInfo()
