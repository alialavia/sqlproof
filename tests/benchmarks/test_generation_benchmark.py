from __future__ import annotations

from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.parse_sql import parse_schema_sql


def test_ecommerce_dataset_generation_benchmark(benchmark) -> None:
    schema = parse_schema_sql(
        """
        CREATE TABLE customers (
          id SERIAL PRIMARY KEY,
          email TEXT NOT NULL UNIQUE
        );

        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          total NUMERIC(10, 2) NOT NULL CHECK (total >= 0)
        );
        """
    )
    strategy = dataset_strategy(schema, sizes={"customers": 25, "orders": 100})

    benchmark(lambda: draw_example(strategy))
