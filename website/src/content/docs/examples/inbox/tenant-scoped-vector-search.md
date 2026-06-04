---
title: Vector search that leaks across tenants
description: A SECURITY DEFINER similarity-search RPC forgets to filter by org_id, returning nearest neighbors from every tenant.
---

## Problem

You ship `find_similar_tickets(ticket_id)` so agents triaging a new ticket see similar past tickets. It runs as `SECURITY DEFINER` because it touches `message_embeddings`. Months later, an agent in org A pulls up a ticket and the "similar tickets" panel shows a customer-support ticket from org B that happens to embed close in vector space.

## The code

```sql
CREATE FUNCTION find_similar_tickets(p_ticket_id UUID, p_k INT DEFAULT 5)
RETURNS TABLE (...) SECURITY DEFINER AS $$
  SELECT m.ticket_id, (me.embedding <-> ...) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  WHERE m.ticket_id <> p_ticket_id
  ORDER BY distance ASC LIMIT p_k;
$$;
```

## Why review misses it

Two failure modes compound here. First, `SECURITY DEFINER` bypasses caller RLS — so RLS on `tickets` doesn't save you. Second, the function reads as a "find the nearest neighbors" query, and reviewers don't typically read those queries asking "do they cross a security boundary?" The org filter belongs in the function body, not in the policy layer.

## The example test that passes

```python
def test_returns_some_neighbors(db, org_with_tickets):
    rows = db.query("SELECT * FROM find_similar_tickets(%s)", org_with_tickets["tickets"][0]["id"])
    assert len(rows) > 0
```

Seeds one org. No cross-tenant leak possible.

## The SqlProof property

```python
@given(data=st.data())
def test_similar_tickets_are_all_in_the_input_org(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 2, "customers": 2, "org_members": 2,
               "tickets": 4, "messages": 4, "message_embeddings": 2},
        columns={"message_embeddings.embedding": vector_strategy(384)},
    ))
    # ... run as a member of the input ticket's org; verify returned ticket_ids are all from that org
    assert cross_tenant == []
```

Two orgs is the minimum that makes the bug visible. Hypothesis generates them.

The `vector_strategy(384)` helper (in `examples/inbox/tests/_helpers.py`) is a workaround for sqlproof issue #69 — once the schema parser handles `vector(N)` natively, the `columns=` override can be dropped.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: vector search leaked across tenants
AssertionError: vector search leaked across tenants: [{'id': UUID('...'), 'org_id': UUID('...')}]
Returned ticket from org B while input ticket was in org A
```

## The fix

Resolve the input ticket's `org_id` in a CTE and filter the search to it:

```sql
WITH input AS (
  SELECT t.org_id, me.embedding FROM tickets t ...
  WHERE t.id = p_ticket_id LIMIT 1
)
... WHERE t.org_id = (SELECT org_id FROM input)
```

## Related

For the general "SECURITY DEFINER bypasses RLS" pattern, see the [RLS bug-classes reference](/guides/supabase-rls-bug-classes/).
