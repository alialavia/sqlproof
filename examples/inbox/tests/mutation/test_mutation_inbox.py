"""Recipe 11: score the inbox suite with mutation testing.

Each meta-test re-introduces one recipe's bug (or a close cousin) into
the FIXED schema as a mutant, runs that recipe's property suite against
a clone database, and requires the suite to kill it. A survivor would
mean the property no longer constrains the behavior it was written for.

Requires a prepared template database with all fix migrations applied
and zero open connections (see the recipe page / README):

    BASE='postgresql://postgres:postgres@127.0.0.1:5433/postgres'
    psql "$BASE" -c 'DROP DATABASE IF EXISTS inbox_proof_template WITH (FORCE)'
    psql "$BASE" -c 'CREATE DATABASE inbox_proof_template TEMPLATE postgres'
    for f in examples/inbox/schema/*.sql; do
      psql "${BASE%/*}/inbox_proof_template" -f "$f"
    done
    export SQLPROOF_TEMPLATE_URL="${BASE%/*}/inbox_proof_template"
    pytest examples/inbox/tests/mutation -m mutation -v

Skips if SQLPROOF_TEMPLATE_URL is unset. The server needs CREATEDB
rights, and the inbox suite must be green against the template — a red
baseline makes every mutant look killed.

Replace/Drop patterns match the function body VERBATIM as written in
the schema file (including whitespace alignment); a pattern that is
absent or ambiguous fails loudly at prepare time, before any database
work.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqlproof import Drop, MutationSet, Replace, run_mutation_tests

pytestmark = [
    pytest.mark.mutation,
    pytest.mark.skipif(
        "SQLPROOF_TEMPLATE_URL" not in os.environ,
        reason="set SQLPROOF_TEMPLATE_URL to a prepared inbox template database",
    ),
]

INBOX = Path(__file__).resolve().parent.parent.parent
SCHEMA = INBOX / "schema"
TESTS = INBOX / "tests"


def _run(mutations: MutationSet, schema_file: Path, kill_suite: str):
    return run_mutation_tests(
        mutations,
        schema_file=schema_file,
        database_url=os.environ["SQLPROOF_TEMPLATE_URL"],
        pytest_args=[str(TESTS / kill_suite), "-q", "-p", "no:cacheprovider"],
        # The variable sqlproof's pytest plugin reads first, so the inner
        # suite's fixtures hit the per-mutant clone, not the dev database.
        env_var="SQLPROOF_DATABASE_URL",
    )


def test_workload_summary_property_kills_join_mutant() -> None:
    # Recipe 7's bug, verbatim: INNER JOIN drops zero-ticket agents.
    mutations = MutationSet.for_function(
        "agent_workload_summary_v2",
        [Replace("LEFT JOIN", "JOIN")],
    )
    result = _run(
        mutations,
        SCHEMA / "009_fix_workload_summary_v2_nulls.sql",
        "test_workload_summary.py",
    )
    result.assert_no_survivors()


def test_ticket_lifecycle_property_kills_reopen_mutant() -> None:
    # Recipe 8's bug: reopen_ticket no longer clears resolved_at.
    # (`resolved_at = resolved_at` keeps the old value — dropping the
    # assignment outright would leave a dangling comma and be rejected
    # as a parse error at prepare time.)
    mutations = MutationSet.for_function(
        "reopen_ticket",
        [Replace("resolved_at = NULL", "resolved_at = resolved_at")],
    )
    result = _run(
        mutations,
        SCHEMA / "010_fix_reopen_ticket.sql",
        "test_ticket_lifecycle.py",
    )
    result.assert_no_survivors()


def test_delete_policy_property_kills_admin_check_mutant() -> None:
    # Recipe 10 routes its DELETE policy through is_admin_in_org().
    # v1 mutates functions only, so weakening the helper is how you
    # score RLS tests: without the role check, ANY org member passes
    # the admin gate and can eject anyone.
    mutations = MutationSet.for_function(
        "is_admin_in_org",
        [
            Drop(" AND role = 'admin'"),
            # An equivalent mutant, declared up front: EXISTS ignores
            # its SELECT list, so no test can ever kill this one.
            Replace(
                "SELECT 1",
                "SELECT 2",
                expect_survives=True,
                reason="EXISTS ignores the SELECT list; semantically identical",
            ),
        ],
    )
    result = _run(
        mutations,
        SCHEMA / "012_add_org_members_delete_policy.sql",
        "test_org_members_delete_policy.py",
    )
    result.assert_no_survivors()


def test_similar_tickets_property_kills_org_filter_mutant() -> None:
    # Recipe 1's bug: the similarity search stops filtering by tenant.
    mutations = MutationSet.for_function(
        "find_similar_tickets",
        [Replace("t.org_id    = (SELECT org_id FROM input)", "TRUE")],
    )
    result = _run(
        mutations,
        SCHEMA / "002_fix_similar_tickets.sql",
        "test_similar_tickets.py",
    )
    result.assert_no_survivors()


def test_dashboard_properties_kill_dashboard_mutants() -> None:
    # Recipe 4's symptom (zero-status buckets vanish), plus the classic
    # count(*) vs count(col): count(*) counts the all-NULL row of an
    # empty LEFT JOIN group, reporting 1 instead of 0.
    mutations = MutationSet.for_function(
        "organization_dashboard",
        [
            Replace("LEFT JOIN", "JOIN"),
            Replace("count(t.id)", "count(*)"),
        ],
    )
    result = _run(
        mutations,
        SCHEMA / "005_fix_dashboard.sql",
        "test_dashboard.py",
    )
    result.assert_no_survivors()
