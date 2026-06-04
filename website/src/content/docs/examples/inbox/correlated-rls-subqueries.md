---
title: Correlated RLS subqueries
description: An EXISTS subquery in an RLS policy that doesn't correlate to the parent row leaks every tenant's data.
---

## Problem

You ship a SELECT policy on `tickets` so that "agents only see their org's tickets." Code-review goes fine; the local test passes. In production, an agent in a different org runs a list query and gets back every other org's tickets.

## The code (`schema/001_initial.sql`)

```sql
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
    )
  );
```

## Why review misses it

The shape "EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid())" reads as "is the caller a member?" — and reviewers pattern-match on that intent. The missing correlation back to `tickets.org_id` is invisible until you ask "a member of *which* org?".

## The example test that passes

```python
def test_agent_sees_their_org_tickets(db, org, ticket):
    with as_rls_user(db, org["owner_id"]):
        rows = db.query("SELECT id FROM tickets WHERE org_id = %s", org["id"])
    assert len(rows) == 1
```

One org, one ticket — the policy returns the row, the test is green. The cross-org leak only surfaces when the test data contains *two* distinct orgs.

## The SqlProof property

```python
from hypothesis import assume, given
from hypothesis import strategies as st

@given(data=st.data())
def test_member_of_org_a_cannot_read_tickets_in_org_b(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 2, "org_members": 2, "customers": 2, "tickets": 2},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        orgs = dataset["organizations"]
        members = dataset["org_members"]
        # Find a member exclusively in org A (not also in org B)
        org_b_user_ids = {m["user_id"] for m in members if m["org_id"] == orgs[1]["id"]}
        org_a_only = [m for m in members
                      if m["org_id"] == orgs[0]["id"] and m["user_id"] not in org_b_user_ids]
        tickets_in_b = [t for t in dataset["tickets"] if t["org_id"] == orgs[1]["id"]]
        assume(org_a_only)
        assume(tickets_in_b)
        with as_rls_user(db, org_a_only[0]["user_id"]):
            visible = db.query("SELECT id FROM tickets WHERE org_id = %s", orgs[1]["id"])
        assert visible == []
```

## The counterexample

```
Property failed: member of org A leaked tickets from org B
Draw 1: organizations=[org_1, org_2], org_members=[{org_1, user_u1}, {org_1, user_u2}],
        tickets=[{org_1, ...}, {org_2, ...}]
```

## The fix (`schema/003_fix_tickets_rls.sql`)

```sql
DROP POLICY "agents see org tickets" ON tickets;
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
        AND org_members.org_id  = tickets.org_id   -- the missing line
    )
  );
```

One line. Two-org property tests catch every version of this bug.
