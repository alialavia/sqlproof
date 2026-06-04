"""Recipe 8: stateful-ticket-lifecycle.

A state machine that cycles a ticket through resolve <-> reopen and
asserts that `resolved_at` is NULL whenever the status is not
'resolved'. The bug surfaces only after the sequence
{resolve -> reopen}: status becomes 'reopened' but resolved_at
remains stale.

Uses the `initial_dataset` ClassVar API to seed a deterministic
single-ticket fixture. The state machine then mutates that ticket
through its lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis.stateful import invariant, rule

from sqlproof.testing import SqlProofStateMachine

# Deterministic UUIDs and a fixed sla_due_at so the dataset is fully
# specified for the state machine.
_ORG_ID      = "00000000-0000-4000-8000-000000000001"
_CUSTOMER_ID = "00000000-0000-4000-8000-000000000002"
_TICKET_ID   = "00000000-0000-4000-8000-000000000003"
_SLA_DUE_AT  = datetime(2026, 12, 31, tzinfo=timezone.utc)


class TicketLifecycleMachine(SqlProofStateMachine):
    initial_dataset = {
        "organizations": [
            {
                "id": _ORG_ID,
                "name": "Test Org",
                "sla_tier": "bronze",
            },
        ],
        "customers": [
            {
                "id": _CUSTOMER_ID,
                "email": "customer@test.invalid",
                "display_name": "Test Customer",
            },
        ],
        "tickets": [
            {
                "id": _TICKET_ID,
                "org_id": _ORG_ID,
                "customer_id": _CUSTOMER_ID,
                "assigned_agent_id": None,
                "status": "open",
                "priority": "med",
                "subject": "Test Ticket",
                "resolved_at": None,
                "sla_due_at": _SLA_DUE_AT,
            },
        ],
    }

    def on_setup(self) -> None:
        self.ticket_id = _TICKET_ID
        # Force a known initial state. The dataset seed sets status='open'
        # and resolved_at=NULL, but be explicit so the invariant has a
        # clean starting point even if Recipe 3's trigger fires.
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
