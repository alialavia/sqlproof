"""Recipe 1: tenant-scoped-vector-search.

`find_similar_tickets` returns the k nearest neighbors by message
embedding distance but never filters by org_id. A ticket in org A
finds matches from org B.
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

# pgvector tradeoffs:
#   data_too_large / too_slow — 384-dim vector serialization overhead per example.
#   filter_too_much — assume(rows) rejects when the sparse 4-ticket/2-embedding
#     dataset produces no neighbors; not fixable without growing the dataset
#     (which would worsen the speed budget above).
#   function_scoped_fixture — supabase_proof is function-scoped to keep
#     dataset isolation per Hypothesis example.
# max_examples kept low for the same reason.
PROOF = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
        HealthCheck.too_slow,
    ],
)


@PROOF
@given(data=st.data())
def test_similar_tickets_are_all_in_the_input_org(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations":      2,
                "customers":          2,
                "org_members":        2,
                "tickets":            4,
                "messages":           4,
                "message_embeddings": 2,
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        # Pick the first org_member and find a ticket in that member's org.
        # This avoids the filtering hot-spot of "find a member for a random ticket."
        member = dataset["org_members"][0]
        tickets_in_org = [
            t for t in dataset["tickets"]
            if t["org_id"] == member["org_id"]
        ]
        assume(tickets_in_org)
        input_ticket = tickets_in_org[0]

        with as_rls_user(db, member["user_id"]):
            rows = db.query(
                "SELECT ticket_id FROM find_similar_tickets(%s::uuid, 5)",
                input_ticket["id"],
            )

        assume(rows)  # if no neighbors found, nothing to check

        returned_ticket_ids = [r["ticket_id"] for r in rows]
        returned_orgs = db.query(
            "SELECT id, org_id FROM tickets WHERE id = ANY(%s::uuid[])",
            returned_ticket_ids,
        )
        input_org_id = str(input_ticket["org_id"])
        cross_tenant = [
            t for t in returned_orgs
            if str(t["org_id"]) != input_org_id
        ]
        assert cross_tenant == [], (
            f"vector search leaked across tenants: {cross_tenant}"
        )
