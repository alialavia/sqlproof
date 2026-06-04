"""Recipe 5: internal-message-rls.

The `messages` SELECT policy gates visibility on parent-ticket access
but never checks `is_internal`. Customers viewing their own ticket
read agent-only internal notes.
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
def test_customer_does_not_see_internal_notes_on_their_own_ticket(
    supabase_proof, data,
) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "tickets": 1,
                "messages": 3,
            },
            columns={
                "messages.is_internal": st.booleans(),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        ticket = dataset["tickets"][0]
        customer = next(
            c for c in dataset["customers"] if c["id"] == ticket["customer_id"]
        )

        # We need at least one internal message in the generated set
        # for this test to be meaningful — otherwise the buggy policy
        # has nothing to leak.
        internal_messages = [m for m in dataset["messages"] if m["is_internal"]]
        assume(internal_messages)

        # Simulate a logged-in customer: a Supabase auth user with a
        # `customer_id` claim. The pool gives us a real auth.users id;
        # we use it as the customer's auth identity.
        rows = db.query(
            r"SELECT id::text FROM auth.users WHERE email LIKE %s ESCAPE '\' LIMIT 1",
            r"sqlproof\_%@test.invalid",
        )
        assert rows, "no sqlproof test users seeded in auth.users — bootstrap missing"
        customer_auth_id = rows[0]["id"]

        with as_rls_user(
            db,
            customer_auth_id,
            extra_claims={"customer_id": str(customer["id"])},
        ):
            visible = db.query(
                "SELECT id, is_internal FROM messages WHERE ticket_id = %s",
                ticket["id"],
            )

        leaked = [m for m in visible if m["is_internal"]]
        assert leaked == [], (
            f"customer leaked internal messages: {leaked}"
        )
