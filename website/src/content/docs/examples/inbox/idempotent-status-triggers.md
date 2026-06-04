---
title: Triggers that aren't idempotent across no-op updates
description: A status-change trigger that doesn't check the transition fires on every edit.
---

## Problem

A trigger sets `tickets.resolved_at = now()` when a ticket is resolved. A few weeks later, an agent edits the subject of an already-resolved ticket to fix a typo. The `resolved_at` jumps forward by three weeks. SLA reporting silently breaks.

## The code

```sql
CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status = 'resolved' THEN
    NEW.resolved_at := now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

## Why review misses it

The trigger reads as "when a ticket is resolved, set the timestamp." Reviewers think in terms of the resolve action, not in terms of every future edit that happens to leave the status set to `'resolved'`.

## The example test that passes

```python
def test_resolving_a_ticket_sets_resolved_at(db, open_ticket):
    db.execute("UPDATE tickets SET status = 'resolved' WHERE id = %s", open_ticket["id"])
    after = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", open_ticket["id"])
    assert after is not None
```

Tests the *transition*; doesn't test the no-op update.

## The SqlProof property

```python
@given(new_subject=st.text(min_size=1, max_size=80))
def test_editing_resolved_ticket_does_not_bump_resolved_at(proof, data, new_subject):
    dataset = data.draw(proof.dataset_strategy(
        sizes={"tickets": 1},
        columns={"tickets.status": st.just("resolved")},
    ))
    with proof.client_for_dataset(dataset) as db:
        t_id = dataset["tickets"][0]["id"]
        before = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", t_id)
        db.execute("UPDATE tickets SET subject = %s WHERE id = %s", new_subject, t_id)
        after  = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", t_id)
        assert after == before
```

The phrasing is "applying the update *does not change* `resolved_at`" — an idempotency property: doing the operation N times should equal doing it once (and zero times changes nothing).

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: resolved_at was bumped by a non-status edit
before=2026-06-03 10:00:00+00, after=2026-06-03 10:00:00.000001+00
```

## The fix

Check that the status *transitioned* into `'resolved'`:

```sql
IF NEW.status = 'resolved'
   AND (OLD IS NULL OR OLD.status IS DISTINCT FROM 'resolved')
THEN
  NEW.resolved_at := now();
END IF;
```
