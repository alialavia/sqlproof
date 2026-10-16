---
title: Table Customization
description: Override column generators while keeping schema-aware datasets.
---

Pass `columns=` to `proof.dataset_strategy()` or the `@sqlproof` decorator to
override selected columns while keeping the rest of the dataset schema-aware.
The keys are `"table.column"`; values can be Hypothesis strategies, fixed values,
or callables receiving a `ColumnContext` and returning a value.

```python
from hypothesis import strategies as st

datasets = proof.dataset_strategy(
    sizes={"products": 3},
    columns={
        "products.price": st.decimals(min_value="0.01", max_value="9999.99", places=2),
        "products.sku": st.from_regex(r"[A-Z]{2}-[0-9]{4}", fullmatch=True),
    },
)
```

Draw a dataset inside a Hypothesis test and use
`proof.client_for_dataset(dataset)` to load it. Overrides must satisfy the
schema's constraints. An override replaces the normal generator, including
CHECK refinement and nullable-column handling. Use `st.just(None)` to force
SQL NULL in a nullable column.

Default-bearing columns are omitted unless requested explicitly. Single-column
primary keys use a separate assignment path; use a [scenario](/guides/scenarios/)
when you need pinned keys and relationships.

`proof.customize()` is currently a placeholder and does not apply overrides.
Use `columns=` for working customizations.

See [custom generators](/guides/custom-generators/) for default sampling limits
and edge-case recipes, and [FK distributions](/guides/fk-distributions/) for
controlling parent selection.
