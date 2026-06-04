"""Recipe 8: stateful-ticket-lifecycle.

A state machine that cycles a ticket through resolve <-> reopen and
asserts that `resolved_at` is NULL whenever the status is not
'resolved'. The bug surfaces only after the sequence
{resolve -> reopen}: status becomes 'reopened' but resolved_at
remains stale.
"""

from __future__ import annotations

from hypothesis.stateful import invariant, rule

from sqlproof.testing import SqlProofStateMachine


class TicketLifecycleMachine(SqlProofStateMachine):
    sizes = {"organizations": 1, "customers": 1, "tickets": 1}

    def on_setup(self) -> None:
        self.ticket_id = self.dataset["tickets"][0]["id"]
        # Force a known initial state: open.
        self.db.execute(
            "UPDATE tickets SET status = 'open', resolved_at = NULL "
            "WHERE id = %s",
            self.ticket_id,
        )

    @rule()
    def resolve(self) -> None:
        self.db.execute(
            "UPDATE tickets SET status = 'resolved' WHERE id = %s",
            self.ticket_id,
        )

    @rule()
    def reopen(self) -> None:
        self.db.execute("SELECT reopen_ticket(%s)", self.ticket_id)

    @rule()
    def edit_subject(self) -> None:
        self.db.execute(
            "UPDATE tickets SET subject = subject || '.' WHERE id = %s",
            self.ticket_id,
        )

    @invariant()
    def non_resolved_status_means_resolved_at_is_null(self) -> None:
        row = self.db.query(
            "SELECT status, resolved_at FROM tickets WHERE id = %s",
            self.ticket_id,
        )[0]
        if row["status"] != "resolved":
            assert row["resolved_at"] is None, (
                f"stale resolved_at: status={row['status']!r}, "
                f"resolved_at={row['resolved_at']}"
            )


def test_ticket_lifecycle_invariant(proof) -> None:
    proof.run_state_machine(TicketLifecycleMachine)
