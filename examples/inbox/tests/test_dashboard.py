"""Recipe 4: outer-joins-and-where.

`organization_dashboard` should return one row per ticket_status enum
value. The buggy implementation drops zero-bucket rows because a WHERE
clause on the right side of the LEFT JOIN collapses it to INNER.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

ALL_STATUSES = {"open", "pending", "resolved", "reopened"}


@PROOF
@given(data=st.data())
def test_dashboard_returns_every_status_bucket(proof, data) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "tickets": st.integers(min_value=0, max_value=5),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]
        rows = db.query(
            "SELECT status, count FROM organization_dashboard(%s)",
            org_id,
        )
        present = {row["status"] for row in rows}
        assert present == ALL_STATUSES, (
            f"dashboard dropped status buckets: missing {ALL_STATUSES - present}"
        )


@PROOF
@given(data=st.data())
def test_dashboard_counts_sum_to_org_ticket_total(proof, data) -> None:
    """Companion sanity invariant: sum of dashboard counts equals total tickets in the org.

    This property holds even with the BUGGY implementation, because dropping
    zero-count buckets doesn't change the sum. It's not a bug detector — it's
    a complementary invariant that demonstrates how a single property test
    doesn't always cover everything. See `test_dashboard_returns_every_status_bucket`
    above for the property that actually catches the LEFT-JOIN-collapsed-to-INNER
    bug.
    """
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={"organizations": 1, "customers": 1, "tickets": 5},
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]
        rows = db.query(
            "SELECT count FROM organization_dashboard(%s)",
            org_id,
        )
        dashboard_total = sum(row["count"] for row in rows)
        actual_total = db.scalar(
            "SELECT count(*) FROM tickets WHERE org_id = %s",
            org_id,
        )
        assert dashboard_total == actual_total
