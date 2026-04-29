---
title: Table Customization
description: Override column strategies and FK distributions per table.
---

`customize()` lets you tune generated data while keeping the rest of the schema
generation automatic.

```python
from hypothesis import strategies as st

proof.customize(
    "products",
    price=st.decimals(min_value="0.01", max_value="9999.99", places=2),
    sku=st.from_regex(r"^[A-Z]{2}-\d{4}$", fullmatch=True),
)
```

## FK Distribution Strategies

Control how child rows pick parent rows:

```python
proof.customize(
    "orders",
    fk_distribution={"customer_id": "zipf"},
)
```

| Strategy                | Behavior                                  |
| ----------------------- | ----------------------------------------- |
| `uniform`               | Equal probability per parent              |
| `zipf`                  | A few parents receive most child rows     |
| `adversarial`           | First, middle, and last parents only      |
| `single`                | All children point to one parent          |
| custom callable         | Return any Hypothesis strategy per FK     |

Custom FK distribution callables receive the available parent primary keys and a
draw context, and must return a Hypothesis strategy so values still shrink.
