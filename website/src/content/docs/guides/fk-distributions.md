---
title: FK Distribution Strategies
description: Control how foreign key references are distributed across parent rows.
---

By default, SqlProof picks parent rows uniformly when generating FK references.
Distribution strategies let you simulate realistic skew or adversarial boundary
cases.

## Built-In Strategies

```python
proof.customize(
    "orders",
    fk_distribution={"customer_id": "uniform"},
)

proof.customize(
    "orders",
    fk_distribution={"customer_id": "zipf"},
)

proof.customize(
    "line_items",
    fk_distribution={"product_id": "adversarial"},
)
```

| Strategy      | Behavior                                      |
| ------------- | --------------------------------------------- |
| `uniform`     | Each parent row has equal probability         |
| `zipf`        | Early parents are referenced more often       |
| `adversarial` | Only first, middle, and last parents are used |
| `single`      | All children point to one parent              |

## Custom Strategy

```python
from hypothesis import strategies as st


def hottest_parent(parent_pks, ctx):
    return st.just(parent_pks[0])


proof.customize(
    "orders",
    fk_distribution={"customer_id": hottest_parent},
)
```

The callable returns a strategy, not a value, so Hypothesis can still shrink
counterexamples.
