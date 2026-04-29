# SqlProof

SqlProof is a Python library for property-based testing of PostgreSQL schemas and SQL
behavior. Developers define properties; SqlProof generates valid datasets from a
Postgres schema and tries to falsify those properties.

## Quick Start

```bash
pip install sqlproof
```

```python
from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"orders": 10}, runs=25)
def test_order_totals_non_negative(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)
```

Run with pytest:

```bash
pytest
```
