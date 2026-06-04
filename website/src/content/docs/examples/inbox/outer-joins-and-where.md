---
title: LEFT JOIN collapsed to INNER by a WHERE clause
description: A dashboard query intends to enumerate every status bucket; a WHERE on the right side silently drops zero-count rows.
---

## Problem

The Org Admin dashboard shows ticket counts by status. The chart renders fine for active orgs. When a new org is created — or an existing org has no `reopened` tickets — the corresponding bucket simply doesn't appear. The frontend assumes a complete enum and renders `undefined`.

## The code

```sql
CREATE FUNCTION organization_dashboard(p_org_id UUID) RETURNS TABLE (status ticket_status, count BIGINT) AS $$
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t ON t.status = s.status
  WHERE t.org_id = p_org_id
  GROUP BY s.status;
$$;
```

## Why review misses it

Engineers know `LEFT JOIN ... WHERE right_side = X` is risky in the abstract — but here the `WHERE` reads as the tenant filter. It scans as "show every status; filter by org." The "filter by org" reads as a constraint on the result, not as a transformation of the join.

## The example test that passes

```python
def test_dashboard_returns_counts(db, org):
    seed_tickets(db, org["id"], statuses=["open", "pending", "resolved", "reopened"])
    rows = db.query("SELECT status, count FROM organization_dashboard(%s)", org["id"])
    assert len(rows) == 4
```

Seeds one ticket in each status. All four buckets present. Test green. The bug only fires when a bucket is empty.

## The SqlProof property

```python
@given(data=st.data())
def test_dashboard_returns_every_status_bucket(proof, data):
    dataset = data.draw(proof.dataset_strategy(
        sizes={"organizations": 1, "customers": 1, "tickets": st.integers(min_value=0, max_value=5)},
    ))
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]
        rows = db.query("SELECT status FROM organization_dashboard(%s)", org_id)
        assert {r["status"] for r in rows} == {"open", "pending", "resolved", "reopened"}
```

A second property asserts `sum(counts) == count(*)` — an even tighter aggregation invariant that holds even for the buggy version (dropping zero-count buckets doesn't change the sum), demonstrating that one property doesn't always cover everything.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: dashboard dropped status buckets: missing {'reopened'}
Dataset: {"organizations": 1, "tickets": 3} — none with status='reopened'
```

## The fix

Move the org filter into the join condition:

```sql
LEFT JOIN tickets t
  ON t.status = s.status
 AND t.org_id = p_org_id
```

Now the LEFT JOIN really is a LEFT JOIN, regardless of how many tickets each bucket contains.
