---
title: FK Distribution Strategies
description: Control foreign key parent selection in property tests and bulk loads.
---

The Hypothesis dataset generator selects existing parents with
`st.sampled_from`. It searches and shrinks examples; it does not promise a
fixed distribution or automatically explore NULL foreign keys when parents
are available.

## Pinning Parent Selection in a Property

For a property that needs every order to belong to the same customer, use a
column override. The callable receives a `ColumnContext`, whose
`rows_by_table` holds already-generated parent rows:

```python
datasets = proof.dataset_strategy(
    sizes={"customers": 3, "orders": 10},
    columns={
        "orders.customer_id": lambda context: context.rows_by_table["customers"][0]["id"],
    },
)
```

This assumes `orders.customer_id` is an ordinary, single-column FK and at
least one customer is generated. The callable returns a value. Return a
Hypothesis strategy directly as a `columns=` entry when the choices do not
depend on the generated parents. To force a missing relationship in a
nullable FK column, use `st.just(None)`.

## Bulk Distributions for Scale Measurements

The bulk loader supports `uniform` (the default) and `zipf` parent selection.
Use Zipf when a few parents should own many child rows:

```python
from sqlproof import postgres
from sqlproof.scale.load import load_dataset

with postgres.connect(database_url, autocommit=True) as conn:
    load_dataset(
        conn,
        proof.schema_info,
        {"customers": 100, "orders": 10000},
        seed=42,
        distribution="zipf",
    )
```

This inserts into the supplied database; use a disposable test database.
`distribution` applies to the load as a whole. These are bulk sampling options;
the Hypothesis generator does not read them from `proof.customize()`.

For property-specific cohorts and explicit relationships, see
[scenarios](/guides/scenarios/). For other overrides and their sampling limits,
see [custom generators](/guides/custom-generators/).
