"""Smoke test: schema loads and every expected public table exists."""

from __future__ import annotations

EXPECTED_TABLES = {
    "organizations",
    "org_members",
    "customers",
    "tickets",
    "messages",
    "ticket_events",
    "tags",
    "ticket_tags",
    "message_embeddings",
    "kb_articles",
    "kb_article_embeddings",
}


def test_all_expected_tables_present(db) -> None:
    rows = db.query(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        """,
    )
    present = {row["tablename"] for row in rows}
    missing = EXPECTED_TABLES - present
    assert not missing, f"missing tables: {sorted(missing)}"


def test_pgvector_extension_available(db) -> None:
    rows = db.query(
        "SELECT extname FROM pg_extension WHERE extname = 'vector'",
    )
    assert rows, "pgvector extension must be installed"
