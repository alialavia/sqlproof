---
title: Custom Generators
description: Override default column generators with Hypothesis strategies.
---

SqlProof maps PostgreSQL types to [Hypothesis](https://hypothesis.works/)
strategies automatically. These generators search a practical subset of each
type, rather than every value PostgreSQL can store. For tighter control, pass
column overrides to `proof.dataset_strategy()` or the `@sqlproof` decorator.

## Basic Override

```python
from hypothesis import strategies as st

datasets = proof.dataset_strategy(
    sizes={"products": 3},
    columns={
        "products.price": st.decimals(min_value="0.01", max_value="9999.99", places=2),
        "products.name": st.text(
            alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
            min_size=1,
            max_size=100,
        ),
    },
)
```

Draw `datasets` inside a Hypothesis test and load it with
`proof.client_for_dataset(dataset)`. An override replaces the column's normal
generator, including automatic NULL handling and CHECK refinement. Choose
values that satisfy the schema. Default-bearing columns need an explicit
override when the property must inspect their generated values; otherwise the
database supplies the default and the dataset omits the column.

## Default Sampling Limits

These defaults describe ordinary, non-key columns without overrides. Recognized
CHECKs narrow the values further. Primary keys, single-column UNIQUE values and
foreign keys follow their own assignment paths: keys are usually deterministic
and foreign keys select existing parent keys. Do not assume those columns
explore the full type domain or nullable foreign keys explore missing links.
Numeric keys and single-column UNIQUE values are enumerated as distinct
multiples of their declared unit, with recognized CHECKs and domain CHECKs
applied. Requests beyond that finite non-NULL domain raise a generation error.
Bulk foreign keys and scenario references use the parent's assignment rules,
including its precision and scale. Other key types can still exceed narrow
type bounds or ignore CHECKs. Ordinary `columns=` overrides do not replace
single-column primary keys.

| PostgreSQL type | Default search domain | Cases to request explicitly |
| --- | --- | --- |
| `smallint`, `integer`, `bigint` | Full signed integer bounds | Application-specific boundaries |
| `numeric(p, s)`, `numeric(p)` | Finite values at the declared scale; omitted scale means zero | `NaN`; input values that PostgreSQL must round |
| Bare `numeric` | -1,000,000 to 1,000,000, two decimal places | Larger magnitudes, finer fractions, `NaN` and infinities |
| `real`, `double precision` | Finite values from -1,000,000 to 1,000,000 unless CHECK bounds change the window | Larger finite magnitudes, `NaN` and infinities |
| `date` | 2000-01-01 through 2054-10-04 | Earlier/later dates |
| `timestamp`, `timestamptz` | 2000-01-01 through 2031-09-09 01:46:40; naive Python datetimes | Timezone offsets, DST transitions, earlier/later instants |
| `time`, `timetz` | Naive Python times within a day | Timezone offsets |
| `interval` | Zero through 10,000,000 seconds (about 115 days) | Negative or longer durations; calendar-month semantics |
| `text`, `citext`, unbounded `varchar` | Up to 255 characters, excluding NUL and surrogates | Longer strings and application-specific formats |
| `varchar(n)`, `char(n)` | Up to `n` characters; `char(n)` draws exactly `n` | Short inputs that exercise `char(n)` padding |
| `bytea` | Zero to 32 bytes | Larger payloads |
| Arrays | One dimension, usually zero to five non-NULL elements | NULL elements, longer or multidimensional arrays, non-default lower bounds |
| Ranges | Finite, nonempty, `[)` | Empty, unbounded, other inclusivity choices |
| `json`, `jsonb` | Booleans, finite floating-point numbers, strings, lists and objects; JSON null inside containers | Top-level JSON null and exact integer numbers |
| `vector(n)` | Components in [-1, 1], six decimal places | Larger components or finer precision |
| Unknown scalar types | Text fallback, up to 255 characters | A strategy valid for the actual extension type |

Array cardinality CHECKs can change the usual length window. Composite fields
use their type generators, without adding NULL field values. Enum generators
choose declared labels.

For arrays of custom domains, the driver may return text elements. Cast to the
base array type in queries (for example, `amounts::numeric[]`) when the property
needs typed values.

Python dates and datetimes cannot represent PostgreSQL's full date range or
its temporal infinity values. Widening a Hypothesis strategy alone does not
add support for those values. Timestamptz values generated without a timezone
are interpreted in the database session's timezone, so use aware datetimes
when the property's contract concerns instants.

For nullable ordinary columns, the Hypothesis generator includes SQL NULL;
there is no fixed NULL probability or guarantee that a particular run draws
it. Assert important NULL behavior with a focused property or an explicit
example. The bulk loader uses a configurable `null_frac` (default 0.1).

Bulk generation shares the practical windows but has a different purpose:
volume for scale measurements. It uses ASCII text, whole-second temporal
values and simple one-entry JSON objects. Its sampled distribution is not a
substitute for a Hypothesis search over edge cases. See
[scale analysis](/guides/scale-analysis/) for bulk overrides.

## Dates, Timezones and Negative Intervals

Use bounds that express the property's contract. For example, an events table
that permits historical dates, aware timestamps and negative adjustments can
opt into them:

```python
from datetime import date, datetime, timedelta

from hypothesis import strategies as st

datasets = proof.dataset_strategy(
    sizes={"events": 3},
    columns={
        "events.day": st.dates(date(1900, 1, 1), date(2100, 12, 31)),
        "events.occurred_at": st.datetimes(
            min_value=datetime(2000, 1, 1),
            max_value=datetime(2030, 12, 31),
            timezones=st.timezones(),
            allow_imaginary=False,
        ),
        "events.adjustment": st.timedeltas(timedelta(days=-365), timedelta(days=365)),
    },
)
```

For a known boundary such as a leap day or repeated DST hour, add an explicit
Hypothesis `@example` to the property that checks that behavior. A broader
strategy makes cases possible; it does not promise to select each boundary.

## SQL NULL, JSON Null and Array Elements

Python `None` denotes SQL NULL. Use `Jsonb(None)` for the JSON scalar `null`,
which is a non-NULL SQL value. These need different assertions: `IS NULL`
versus `jsonb_typeof(payload) = 'null'`.

```python
from hypothesis import strategies as st

from sqlproof.postgres import Jsonb

datasets = proof.dataset_strategy(
    sizes={"events": 3},
    columns={
        "events.payload": st.one_of(
            st.none(), st.just(Jsonb(None)), st.integers(-(2**63), 2**63 - 1),
        ),
        "events.tags": st.lists(st.one_of(st.none(), st.integers(-100, 100)), max_size=20),
    },
)
```

Here `payload` must be nullable `jsonb` and `tags` must accept integer elements
including NULL. To force SQL NULL in a nullable column for every row, pass
`st.just(None)` as its override. To test empty tables or larger datasets,
choose those row counts in `sizes`; column strategies do not choose them.

## Well-Known Strategies

SqlProof also ships helpers for common string domains:

```python
from sqlproof.generators.well_known import emails, slugs, urls

datasets = proof.dataset_strategy(
    sizes={"customers": 3, "content": 3},
    columns={
        "customers.email": emails(),
        "content.slug": slugs(max_length=64),
        "content.canonical_url": urls(include_fragment=False),
    },
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
