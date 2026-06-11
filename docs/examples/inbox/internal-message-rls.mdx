---
title: Internal messages visible to customers
description: A policy that says "visible with parent ticket" doesn't gate on `is_internal`, leaking agent notes to the customer.
---

## Problem

Agents leave private triage notes on tickets (`is_internal = true`) — "this customer is angry, route to senior support." The customer reads them through the public ticket-detail API.

## The code

```sql
CREATE POLICY "messages visible with parent ticket" ON messages
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM tickets t
      WHERE t.id = messages.ticket_id
        AND (
          EXISTS (SELECT 1 FROM org_members om
                  WHERE om.user_id = auth.uid() AND om.org_id = t.org_id)
          OR nullif(auth.jwt() ->> 'customer_id', '')::uuid = t.customer_id
        )
    )
  );
```

## Why review misses it

The mental model is "messages inherit visibility from the parent ticket." That's true for agents — and almost true for customers. The exception (`is_internal`) is invisible in the policy.

## The example test that passes

```python
def test_customer_sees_their_messages(db, ticket, customer_message):
    with as_customer(db, ticket["customer_id"]):
        rows = db.query("SELECT id FROM messages WHERE ticket_id = %s", ticket["id"])
    assert len(rows) == 1
```

Seeds one customer message; doesn't seed an internal note; passes.

## The SqlProof property

```python
from hypothesis import assume

dataset = data.draw(supabase_proof.dataset_strategy(
    sizes={"tickets": 1, "messages": 3},
    columns={"messages.is_internal": st.booleans()},
))
with supabase_proof.client_for_dataset(dataset) as db:
    internal = [m for m in dataset["messages"] if m["is_internal"]]
    assume(internal)
    with as_rls_user(db, customer_auth_id, extra_claims={"customer_id": str(customer["id"])}):
        visible = db.query("SELECT id, is_internal FROM messages WHERE ticket_id = %s", ticket["id"])
    assert [m for m in visible if m["is_internal"]] == []
```

Notice the `columns={"messages.is_internal": st.booleans()}` override — `is_internal` has a `DEFAULT false`, so the dataset generator omits it; we have to opt in for the test to read it. And `assume(internal)` discards runs where Hypothesis happens to generate zero internal messages, since the bug can't leak what doesn't exist.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: customer leaked internal messages
Row context: ticket=t1, customer=c1, messages=[m1(is_internal=true)]
```

## The fix

Add the missing gate to the customer branch of the USING clause:

```sql
OR (
  (auth.jwt() ->> 'customer_id')::uuid = t.customer_id
  AND messages.is_internal = false
)
```
