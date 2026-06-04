---
title: Optimizing a query without changing its behavior
description: An equivalence property catches a JOIN-vs-LEFT-JOIN bug that silently drops zero-ticket agents from a dashboard rewrite.
---

## Problem

`agent_workload_summary(org_id)` returns one row per agent in the org with their open count, pending count, and SLA breach count. It uses one correlated subquery per metric — readable, but slow as `tickets` grows. A senior engineer rewrites it as a single JOIN with `FILTER` aggregations. The query plan is much better. After deployment, a new agent who joins the team but hasn't been assigned any tickets simply doesn't appear on the workload dashboard — they're invisible.

## v1: the slow version

```sql
SELECT
  m.user_id,
  (SELECT count(*) FROM tickets t WHERE t.assigned_agent_id = m.user_id AND t.status = 'open')    AS open_count,
  ...
FROM org_members m WHERE m.org_id = p_org_id AND m.role = 'agent';
```

The correlated subqueries return `0` when the agent has no matching tickets, but the **outer query** always returns a row for every agent — because the only `FROM` clause is `org_members`.

## v2: the optimization candidate

```sql
SELECT
  m.user_id,
  count(*) FILTER (WHERE t.status = 'open')    AS open_count,
  ...
FROM org_members m
JOIN tickets t ON t.assigned_agent_id = m.user_id    -- subtle bug
WHERE m.org_id = p_org_id AND m.role = 'agent'
GROUP BY m.user_id;
```

`JOIN tickets` is an INNER JOIN. Agents without tickets disappear. The reviewer reads "join agents to their tickets, group, aggregate" and never asks "what about agents without tickets?".

## The example test (passing)

```python
def test_v2_returns_counts(db, agent_with_tickets):
    rows = db.query("SELECT * FROM agent_workload_summary_v2(%s)", agent_with_tickets["org_id"])
    assert rows[0]["open_count"] >= 0
```

Doesn't compare v1 to v2 at all. Doesn't try an agent with zero tickets.

## The SqlProof property

```python
@given(data=st.data())
def test_workload_summary_v1_equivalent_to_v2(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 1, "customers": 1, "org_members": 3, "tickets": st.integers(min_value=0, max_value=10)},
        columns={"org_members.role": st.just("agent")},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        if not _v2_loaded(db):
            pytest.skip("apply 008_add_workload_summary_v2.sql first")
        v1 = sorted(db.query("SELECT * FROM agent_workload_summary_v1(%s)", org_id), key=...)
        v2 = sorted(db.query("SELECT * FROM agent_workload_summary_v2(%s)", org_id), key=...)
        assert v1 == v2
```

The skip guard handles the realistic case: until v2 is shipped via its migration, the equivalence test has nothing to compare against. Once the migration runs, the test starts gating CI.

## The counterexample

Illustrative — Hypothesis would print the actual draw and assertion:

```
Property failed: v1 != v2
  v1=[(agent1, 0, 0, 0), (agent2, 1, 0, 0), (agent3, 0, 0, 0)]
  v2=[              (agent2, 1, 0, 0)              ]
```

Two agents (agent1 and agent3) have no tickets. v1 includes them with zero counts; v2 drops them entirely.

## The fix

Change `JOIN` to `LEFT JOIN`:

```sql
FROM org_members m
LEFT JOIN tickets t ON t.assigned_agent_id = m.user_id
```

The LEFT JOIN preserves the org_members row even when no tickets match; `count(*) FILTER (...)` then returns 0 over the one-row group (because the filter evaluates to NULL/false on the all-NULL ticket fields), matching v1's contract.

## Lifecycle: when to write and when to delete

This is the recipe's most distinctive teaching beat. Equivalence properties are **scaffolding**, not forever-tests.

1. **Local, during the refactor PR.** Engineer writes v2 and this property in the same commit, iterates until Hypothesis can't find a divergence.
2. **CI on the PR (required check).** Hypothesis runs more examples than the engineer ran locally; the persisted counterexample database under `.sqlproof/failures/` travels with the PR.
3. **CI on `main` during the deprecation window.** v1 and v2 both ship; callers migrate from v1 to v2 over ~one week; this property runs on every commit during the window, catching anyone who tweaks v2 in an unrelated PR and silently breaks equivalence.
4. **Deleted with v1.** Once callers are off v1 and v1 is dropped, the property goes too. Keeping v1 alive just to host the property creates perpetual dead code.

**The exception**: deprecation views, dual-writes, compatibility shims across a versioned API boundary. There the property really is forever.

The other recipes in this section guard *permanent* invariants. This one guards a *transient* one — and that's the point.
