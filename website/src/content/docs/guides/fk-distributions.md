---
title: FK Distribution Strategies
description: Control how foreign key references are distributed across parent rows.
---

By default, SqlProof picks parent rows uniformly at random when generating FK references. Distribution strategies let you change this to simulate more realistic or adversarial data patterns.

## Why It Matters

Real-world databases are rarely uniform. A small set of customers places the majority of orders. If your queries have issues under skewed load, uniform random data might never surface them.

## Available Strategies

### `uniform` (default)

Each parent row has equal probability of being referenced.

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'uniform' },
});
```

### `zipf`

References are skewed: the first parent is referenced most often, following a Zipf distribution (weight ∝ 1/(rank+1)). Simulates realistic hot-row scenarios.

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});
```

With 5 parent rows, approximate probabilities: Row 1: ~44%, Row 2: ~22%, Row 3: ~15%, Row 4: ~11%, Row 5: ~7%.

### `adversarial`

Only picks from the first, middle, and last parent rows — boundary stress testing.

```typescript
proof.customize('line_items', {
  fkDistribution: { product_id: 'adversarial' },
});
```

## Combining Strategies

```typescript
proof.customize('line_items', {
  fkDistribution: {
    order_id: 'zipf',
    product_id: 'adversarial',
  },
});
```

## Full Example

```typescript
const proof = await SqlProof.connect({ schemaFile: './schema.sql' });

proof
  .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
  .customize('line_items', {
    fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
  });

await proof.invariant('FK integrity holds under skewed load', {
  generate: { customers: 5, orders: 20, products: 5, line_items: 100 },
  query: `
    SELECT li.id FROM line_items li
    LEFT JOIN orders o ON li.order_id = o.id
    WHERE o.id IS NULL
  `,
  expectEmpty: true,
  runs: 50,
});

await proof.disconnect();
```
