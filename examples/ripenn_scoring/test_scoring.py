from __future__ import annotations

from pathlib import Path

from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file(Path(__file__).with_name("schema.sql"))


@sqlproof(proof, sizes={"prompts": 10}, runs=5)
def test_total_scores_are_non_negative(db) -> None:
    rows = db.query("SELECT total_score FROM prompts")
    assert all(row["total_score"] >= 0 for row in rows)
