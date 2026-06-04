"""Recipe 7: equivalent-query-optimization.

Equivalence property: for any generated dataset,
`agent_workload_summary_v1(org)` and `agent_workload_summary_v2(org)`
return the same multiset of rows. The v2 candidate is added by
migration 008; this test skips cleanly if it isn't loaded yet.

Uses `supabase_proof` (not `proof`) because `org_members.user_id`
is a FK to `auth.users`, which requires the Supabase-flavored fixture
to seed and register the external table for data generation.

NOTE: this is a "scaffolding" test — short-lived. Real-world usage:
write it during the refactor PR, keep it green-gating CI through the
deprecation window, delete it once v1 is dropped. See the recipe page
for the full lifecycle.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _v2_loaded(db) -> bool:
    return bool(
        db.scalar(
            "SELECT to_regprocedure('public.agent_workload_summary_v2(uuid)') "
            "IS NOT NULL",
        ),
    )


def _sorted_rows(rows: list[dict]) -> list[tuple]:
    return sorted(
        (
            str(r["user_id"]),
            r["open_count"],
            r["pending_count"],
            r["sla_breach_count"],
        )
        for r in rows
    )


@PROOF
@given(data=st.data())
def test_workload_summary_v1_equivalent_to_v2(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "org_members": 3,
                "tickets": st.integers(min_value=0, max_value=10),
            },
            columns={
                "org_members.role": st.just("agent"),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        if not _v2_loaded(db):
            pytest.skip("apply 008_add_workload_summary_v2.sql first")

        org_id = dataset["organizations"][0]["id"]
        v1 = _sorted_rows(
            db.query(
                "SELECT * FROM agent_workload_summary_v1(%s)",
                org_id,
            ),
        )
        v2 = _sorted_rows(
            db.query(
                "SELECT * FROM agent_workload_summary_v2(%s)",
                org_id,
            ),
        )
        assert v1 == v2, f"v1 != v2:\n  v1={v1}\n  v2={v2}"
