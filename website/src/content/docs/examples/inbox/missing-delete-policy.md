---
title: Overly permissive DELETE policy
description: A `USING (true)` DELETE policy lets any authenticated user delete any row — including admins from orgs they aren't part of.
---

## Problem

A viewer issues `DELETE FROM org_members WHERE org_id = 'X' AND user_id = '<admin>'` and silently ejects an admin from an org they don't have admin rights in.

## The code

```sql
CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (true);
```

## Why review misses it

The reviewer reads the SELECT and UPDATE policies (which are correctly constrained) and assumes consistency. The DELETE policy was added later "to fix a flaky test" and quietly shipped without the same constraints.

## The example test that passes

```python
def test_member_can_remove_themselves(db, admin_member):
    with as_rls_user(db, admin_member["user_id"]):
        db.execute("DELETE FROM org_members WHERE user_id = %s", admin_member["user_id"])
    remaining = db.scalar("SELECT count(*) FROM org_members WHERE user_id = %s", admin_member["user_id"])
    assert remaining == 0
```

Confirms the happy path. Doesn't probe whether the policy *should have stopped* a wider class of deletes.

## The SqlProof property

```python
dataset = data.draw(supabase_proof.dataset_strategy(
    sizes={"organizations": 1, "org_members": 2},
    columns={"org_members.role": st.sampled_from(["viewer", "admin"])},
))
with supabase_proof.client_for_dataset(dataset) as db:
    viewers = [m for m in dataset["org_members"] if m["role"] == "viewer"]
    admins  = [m for m in dataset["org_members"] if m["role"] == "admin"]
    assume(viewers)
    assume(admins)
    viewer, admin = viewers[0], admins[0]

    with as_rls_user(db, viewer["user_id"]):
        with db.savepoint():
            try:
                db.execute("DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                          admin["org_id"], admin["user_id"])
            except Exception:
                pass

    still_present = db.scalar(
        "SELECT count(*) FROM org_members WHERE org_id = %s AND user_id = %s",
        admin["org_id"], admin["user_id"],
    )
    assert still_present == 1
```

Same idea as recipe 9: assert the *post-state* of a malicious write, not the return value. Wrap the operation in `db.savepoint()` so a policy violation doesn't poison the outer transaction.

**Note on SELECT policy interaction**: In raw Postgres, a DELETE policy's USING clause is combined with the table's SELECT policies when filtering target rows. This means `USING (true)` is only exploitable when the attacker can also *see* the target row via the SELECT policy. The inbox schema ships a co-member visibility SELECT policy (`is_member_in_org` SECURITY DEFINER helper) alongside the buggy DELETE policy, making the attack observable in property tests.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: viewer deleted admin's membership; rows remaining: 0
Dataset: org_members=[{role: viewer, user_id: u1}, {role: admin, user_id: u2}]
```

## The fix

Add the constraints that should have shipped with the original policy. Because the admin-check subquery would query `org_members` from within an `org_members` RLS policy (causing infinite recursion), the check is routed through a `SECURITY DEFINER` helper:

```sql
CREATE OR REPLACE FUNCTION is_admin_in_org(p_org_id UUID, p_user_id UUID)
  RETURNS BOOLEAN
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM org_members
    WHERE user_id = p_user_id AND org_id = p_org_id AND role = 'admin'
  );
$$;

CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (
    org_members.user_id = auth.uid()
    OR is_admin_in_org(org_members.org_id, auth.uid())
  );
```

This allows members to remove themselves (self-leave) and admins to remove any member, while blocking viewers from ejecting other members.

See also [Mass assignment without WITH CHECK](mass-assignment-without-with-check) — the same blind spot, different operation.
