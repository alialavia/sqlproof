from __future__ import annotations

from pathlib import Path

from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file(Path(__file__).with_name("schema.sql"))


@sqlproof(proof, sizes={"customers": 3, "orders": 10}, runs=5)
def test_order_totals_non_negative(db) -> None:
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)


@sqlproof(proof, sizes={"customers": 3, "orders": 10}, runs=5)
def test_orders_reference_existing_customers(db) -> None:
    data = db.get_generated_data()
    customer_ids = {row["id"] for row in data["customers"]}
    assert all(row["customer_id"] in customer_ids for row in data["orders"])
