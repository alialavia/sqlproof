"""Recipe 2: correlated-rls-subqueries.

The "agents see org tickets" policy on `tickets` uses an EXISTS
subquery that filters by `auth.uid()` but never correlates to
`tickets.org_id`. Result: any member of any org can read every
org's tickets.
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_member_of_org_a_cannot_read_tickets_in_org_b(
    supabase_proof, data,
) -> None:
    """Property: a viewer in org A sees zero rows from org B's tickets."""

    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations": 2,
                "org_members": 2,
                "customers": 2,
                "tickets": 2,
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        orgs = dataset["organizations"]
        members = dataset["org_members"]

        # Find a member of org A who is NOT also in org B, and a ticket in org B.
        # (A user can be in both orgs — the policy must allow that. We need a user
        # who is exclusively in org A to test the isolation property.)
        org_b_user_ids = {m["user_id"] for m in members if m["org_id"] == orgs[1]["id"]}
        org_a_only_members = [
            m for m in members
            if m["org_id"] == orgs[0]["id"] and m["user_id"] not in org_b_user_ids
        ]
        tickets_in_b = [t for t in dataset["tickets"] if t["org_id"] == orgs[1]["id"]]
        assume(org_a_only_members)
        assume(tickets_in_b)
        org_a_member = org_a_only_members[0]

        with as_rls_user(db, org_a_member["user_id"]):
            visible = db.query(
                "SELECT id, org_id FROM tickets WHERE org_id = %s",
                orgs[1]["id"],
            )
        assert visible == [], (
            f"member of org A leaked tickets from org B: {visible}"
        )
