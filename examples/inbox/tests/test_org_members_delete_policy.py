"""Recipe 10: missing-delete-policy.

The DELETE policy on `org_members` was shipped with `USING (true)`,
meaning any authenticated user can delete any row. A viewer in org A
can eject an admin from org A — or, worse, eject members from orgs
they aren't part of at all.
"""

from __future__ import annotations

import contextlib

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
def test_viewer_cannot_delete_admin_in_same_org(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={"organizations": 1, "org_members": 2},
            columns={
                "org_members.role": st.sampled_from(["viewer", "admin"]),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        members = dataset["org_members"]
        viewers = [m for m in members if m["role"] == "viewer"]
        admins  = [m for m in members if m["role"] == "admin"]
        assume(viewers)
        assume(admins)
        viewer, admin = viewers[0], admins[0]

        with (
            as_rls_user(db, viewer["user_id"]),
            db.savepoint(),
            contextlib.suppress(Exception),
        ):
            db.execute(
                "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                admin["org_id"], admin["user_id"],
            )

        still_present = db.scalar(
            "SELECT count(*) FROM org_members WHERE org_id = %s AND user_id = %s",
            admin["org_id"], admin["user_id"],
        )
        assert still_present == 1, (
            f"viewer deleted admin's membership; rows remaining: {still_present}"
        )


@PROOF
@given(data=st.data())
def test_member_of_org_a_cannot_delete_member_of_org_b(supabase_proof, data) -> None:
    """Cross-org case: a member of one org can't delete a member of another."""
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={"organizations": 2, "org_members": 2},
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        orgs = dataset["organizations"]
        members = dataset["org_members"]

        # Find an attacker in org A and a victim in org B with different user_ids
        a_members = [m for m in members if m["org_id"] == orgs[0]["id"]]
        b_members = [m for m in members if m["org_id"] == orgs[1]["id"]]
        assume(a_members)
        assume(b_members)
        attacker = a_members[0]
        victim = next(
            (m for m in b_members if m["user_id"] != attacker["user_id"]),
            None,
        )
        assume(victim is not None)

        with (
            as_rls_user(db, attacker["user_id"]),
            db.savepoint(),
            contextlib.suppress(Exception),
        ):
            db.execute(
                "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                victim["org_id"], victim["user_id"],
            )

        still_present = db.scalar(
            "SELECT count(*) FROM org_members WHERE org_id = %s AND user_id = %s",
            victim["org_id"], victim["user_id"],
        )
        assert still_present == 1, (
            f"cross-org delete succeeded; victim rows remaining: {still_present}"
        )
