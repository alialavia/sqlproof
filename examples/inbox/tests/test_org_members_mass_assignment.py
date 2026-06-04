"""Recipe 9: mass-assignment-without-with-check.

The "members manage their own row" UPDATE policy on `org_members`
restricts *which row* a member can touch (`USING ... user_id =
auth.uid()`) but doesn't constrain *what columns* they can change.
A viewer self-promotes to admin.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_viewer_cannot_self_promote_to_admin(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={"organizations": 1, "org_members": 1},
            columns={
                "org_members.role": st.just("viewer"),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        member = dataset["org_members"][0]

        with as_rls_user(db, member["user_id"]):
            try:
                with db.savepoint():
                    db.execute(
                        "UPDATE org_members SET role = 'admin' "
                        "WHERE org_id = %s AND user_id = %s",
                        member["org_id"], member["user_id"],
                    )
            except Exception:
                # The WITH CHECK clause may raise on a forbidden update.
                # That's also a valid outcome — the post-state check
                # below verifies the row stayed at 'viewer' either way.
                # Using db.savepoint() ensures the transaction stays open
                # even when the UPDATE is rolled back by the policy violation.
                pass

        # Read back as the superuser (RLS bypassed for verification).
        role_after = db.scalar(
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s",
            member["org_id"], member["user_id"],
        )
        assert role_after == "viewer", (
            f"viewer self-promoted to {role_after!r}"
        )
