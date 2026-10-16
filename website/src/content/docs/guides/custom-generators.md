---
title: Custom Generators
description: Override default column generators with Hypothesis strategies.
---

SqlProof maps PostgreSQL types to [Hypothesis](https://hypothesis.works/)
strategies automatically. For tighter control, override columns via
`proof.customize()`.

## Basic Override

```python
from hypothesis import strategies as st

proof.customize(
    "products",
    price=st.decimals(min_value="0.01", max_value="9999.99", places=2),
    name=st.text(min_size=1, max_size=100),
)
```

## Well-Known Strategies

SqlProof also ships helpers for common string domains:

```python
from sqlproof.generators.well_known import emails, slugs, urls

proof.customize(
    "customers",
    email=emails(),
)

proof.customize(
    "content",
    slug=slugs(max_length=64),
    canonical_url=urls(include_fragment=False),
)
```

## Range Edge Cases

Schema-derived range values are finite, nonempty and use `[)` bounds. To
exercise empty ranges and missing lower or upper bounds, use `ranges()` as
a column override. The endpoint strategy must match the PostgreSQL subtype,
and the schema must admit the range shapes being generated.

```python
from hypothesis import strategies as st

from sqlproof import sqlproof
from sqlproof.generators.well_known import ranges


@sqlproof(
    proof,
    sizes={"bookings": 3},
    columns={"bookings.slots": ranges(st.integers(-100, 100))},
)
def test_ranges_survive_loading(db, dataset):
    # bookings.slots is int4range. PostgreSQL should preserve emptiness.
    stored = db.query("SELECT isempty(slots) AS empty FROM bookings")
    assert sum(row["empty"] for row in stored) == sum(
        row["slots"].isempty for row in dataset["bookings"]
    )
```

`ranges()` can generate empty, finite, lower-unbounded, upper-unbounded and
fully unbounded values. Finite endpoints are ordered, and equal endpoints
produce an empty range without filtering out the draw. The default generator
is unchanged; this helper opts a particular property into those cases.

## Type Mapping Examples

| PostgreSQL type | Default strategy |
| --------------- | ---------------- |
| `integer`       | `st.integers(-2147483648, 2147483647)` |
| `bigint`        | `st.integers(-(2**63), 2**63 - 1)` |
| `boolean`       | `st.booleans()` |
| `text`          | `st.text(max_size=255)` |
| `varchar(n)`    | `st.text(max_size=n)` |
| `uuid`          | `st.uuids()` |
| `jsonb`         | Recursive JSON value strategies |
| enum types      | `st.sampled_from(enum_values)` |
