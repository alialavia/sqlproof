"""Recipe 3: idempotent-status-triggers.

The trigger sets `resolved_at = now()` whenever NEW.status is
'resolved', including on edits that don't change status. The
property: editing a resolved ticket's subject leaves `resolved_at`
unchanged.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(
    data=st.data(),
    new_subject=st.text(min_size=1, max_size=80).filter(lambda s: "'" not in s),
)
def test_editing_resolved_ticket_does_not_bump_resolved_at(
    proof, data, new_subject,
) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={"organizations": 1, "customers": 1, "tickets": 1},
            columns={
                "tickets.status": st.just("resolved"),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        ticket_id = dataset["tickets"][0]["id"]

        # Capture resolved_at after dataset insert.
        before = db.scalar(
            "SELECT resolved_at FROM tickets WHERE id = %s",
            ticket_id,
        )

        # Edit a non-status field.
        db.execute(
            "UPDATE tickets SET subject = %s WHERE id = %s",
            new_subject, ticket_id,
        )

        after = db.scalar(
            "SELECT resolved_at FROM tickets WHERE id = %s",
            ticket_id,
        )
        assert after == before, (
            f"resolved_at was bumped by a non-status edit: "
            f"before={before}, after={after}"
        )
