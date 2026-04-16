---
title: TableCustomization
description: Override column generators and FK distribution strategies per table.
---

`TableCustomization` is passed to `proof.customize(table, overrides)`.

```typescript
interface TableCustomization {
  fkDistribution?: Record<string, FkDistributionStrategy>;
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';
```

## Custom Column Generators

Override the default generator for any column with a [fast-check](https://github.com/dubzzz/fast-check) arbitrary:

```typescript
import fc from 'fast-check';

proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
  sku: fc.stringMatching(/^[A-Z]{2}-\d{4}$/),
});
```

## FK Distribution Strategies

Control how foreign key values are assigned when referencing parent rows:

```typescript
proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});
```

| Strategy | Behavior | Best for |
|---|---|---|
| `uniform` (default) | Equal probability per parent | General coverage |
| `zipf` | First parents get many children; later ones few | Realistic skewed data |
| `adversarial` | Only picks first, middle, and last parent | Boundary stress testing |

## Fluent Chaining

`customize()` returns `this`, enabling chaining:

```typescript
proof
  .customize('products', { price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }) })
  .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
  .customize('line_items', { fkDistribution: { order_id: 'zipf', product_id: 'adversarial' } });
```

Multiple calls to `customize()` for the same table are merged — later calls add to earlier ones.
