"""`scale_analysis`'s usage-error path -- no database needed.

A `SqlProof` built from a schema file (rather than a connection_string)
has nowhere for the sweep to run TRUNCATE/COPY/EXPLAIN against, so
`scale_analysis` must refuse before ever attempting a connection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sqlproof.core import SqlProof
from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale import scale_analysis

SCHEMA_SQL = "CREATE TABLE widgets (id bigint PRIMARY KEY, name text NOT NULL);"


def test_scale_analysis_without_a_connection_string_raises_usage_error(
    tmp_path: Path,
) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    proof = SqlProof.from_schema_file(schema_file)

    with pytest.raises(SqlProofUsageError, match="connection_string"):
        scale_analysis(proof, "public.f", sizes={"widgets": 10})
