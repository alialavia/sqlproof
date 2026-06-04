---
title: Mass assignment — UPDATE policies without WITH CHECK
description: A member with permission to edit their own row can change *any column* of that row, including their role.
---

## Problem

You ship a policy: "members can edit their own row in `org_members`." A viewer issues `UPDATE org_members SET role = 'admin' WHERE user_id = auth.uid()` and silently becomes an admin.

## The code

```sql
CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING (org_members.user_id = auth.uid());
```

## Why review misses it

`USING (user_id = auth.uid())` reads as "members can only touch *their own* row" — and that's true. The blind spot is between "which rows can they touch" (USING) and "what state can the row end up in" (WITH CHECK). Reviewers conflate the two.

## The example test that passes

```python
def test_member_can_update_their_display_field(db, viewer_member):
    with as_rls_user(db, viewer_member["user_id"]):
        db.execute("UPDATE org_members SET role = role WHERE user_id = %s", viewer_member["user_id"])
    # No exception raised — policy permits the update.
```

The test confirms members can update — but doesn't check *what they can change*.

## The SqlProof property

```python
@given(data=st.data())
def test_viewer_cannot_self_promote_to_admin(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 1, "org_members": 1},
        columns={"org_members.role": st.just("viewer")},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        member = dataset["org_members"][0]
        with as_rls_user(db, member["user_id"]):
            try:
                with db.savepoint():
                    db.execute("UPDATE org_members SET role = 'admin' WHERE org_id = %s AND user_id = %s", member["org_id"], member["user_id"])
            except Exception:
                pass   # the WITH CHECK may raise; post-state check below is what matters
        role_after = db.scalar("SELECT role FROM org_members WHERE org_id = %s AND user_id = %s", member["org_id"], member["user_id"])
        assert role_after == "viewer"
```

**The key idea**: assert the *post-state*, not the return value. The UPDATE may raise (with the fix) or quietly apply (without). The only evidence of whether the bug is present is in the row.

Note: `db.savepoint()` is used around the UPDATE so that if the WITH CHECK raises a policy-violation error, the transaction stays open for the post-state read. Without it the aborted transaction would prevent the verification query from running.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: viewer self-promoted to 'admin'
Dataset: {"org_members": [{role: "viewer", ...}]}
```

## The fix

Add `WITH CHECK` that pins the new row's role to `'viewer'` — the new row must still be a viewer:

```sql
WITH CHECK (
  user_id = auth.uid()
  AND role = 'viewer'
)
```

This is enum-stable: any future role added to the `member_role` enum is automatically denied without updating this policy. Role *promotions* must go through a `SECURITY DEFINER` admin function instead — that's the standard Supabase pattern.

See also [Missing DELETE policy](missing-delete-policy) — the sibling write-side RLS bug.
