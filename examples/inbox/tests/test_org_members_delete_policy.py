"""Recipe 10: missing-delete-policy.

The DELETE policy on `org_members` was shipped with `USING (true)`,
meaning any authenticated user can delete any row. A viewer in org A
can eject an admin from org A — or, worse, eject members from orgs
they aren't part of at all.
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

        with as_rls_user(db, viewer["user_id"]):
            with db.savepoint():
                try:
                    db.execute(
                        "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                        admin["org_id"], admin["user_id"],
                    )
                except Exception:
                    pass

        still_present = db.scalar(
            "SELECT count(*) FROM org_members WHERE org_id = %s AND user_id = %s",
            admin["org_id"], admin["user_id"],
        )
        assert still_present == 1, (
            f"viewer deleted admin's membership; rows remaining: {still_present}"
        )
