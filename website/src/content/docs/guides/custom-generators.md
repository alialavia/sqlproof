---
title: Custom Generators
description: Override default column generators with fast-check arbitraries.
---

SqlProof maps PostgreSQL types to [fast-check](https://github.com/dubzzz/fast-check) arbitraries automatically. For tighter control — realistic emails, specific numeric ranges, domain-constrained strings — override them via `proof.customize()`.

## Basic Override

```typescript
import fc from 'fast-check';

proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
});
```

## Common Patterns

### Numeric ranges

```typescript
proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  stock: fc.integer({ min: 0, max: 10000 }),
  discount_pct: fc.float({ min: 0, max: 0.5, noNaN: true }),
});
```

### Constrained strings

```typescript
proof.customize('customers', {
  email: fc.emailAddress(),
  name: fc.string({ minLength: 2, maxLength: 100 }),
});
```

### Picking from a fixed set

```typescript
proof.customize('orders', {
  currency: fc.constantFrom('USD', 'EUR', 'GBP'),
  region: fc.constantFrom('us-east', 'us-west', 'eu-central'),
});
```

### Dates in a specific range

```typescript
proof.customize('orders', {
  created_at: fc.date({
    min: new Date('2020-01-01'),
    max: new Date('2024-12-31'),
    noInvalidDate: true,
  }),
});
```

## Default Type Mappings

| PostgreSQL Type | Default Arbitrary |
|---|---|
| `integer`, `int4` | `fc.integer({ min: -2147483648, max: 2147483647 })` |
| `bigint` | `fc.bigInt()` |
| `smallint` | `fc.integer({ min: -32768, max: 32767 })` |
| `numeric(p,s)`, `decimal` | `fc.float()` scaled to precision |
| `real`, `float4` | `fc.float({ noNaN: true, noDefaultInfinity: true })` |
| `double precision` | `fc.double({ noNaN: true, noDefaultInfinity: true })` |
| `boolean` | `fc.boolean()` |
| `text` | `fc.string({ unit: 'grapheme', maxLength: 255 })` |
| `varchar(n)` | `fc.string({ unit: 'grapheme', maxLength: n })` |
| `uuid` | `fc.uuid()` |
| `timestamp`, `timestamptz` | `fc.date({ noInvalidDate: true })` clamped to 1970–2099 |
| `date` | `fc.date()` formatted as `YYYY-MM-DD` |
| `json`, `jsonb` | `fc.jsonValue()` |
| `enum` types | `fc.constantFrom(...enumValues)` |
| `integer[]`, etc. | `fc.array()` of base type |
