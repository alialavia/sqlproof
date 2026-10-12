"""Switching roles must keep Supavisor's ``.<project-ref>`` routing suffix.

Through ``*.pooler.supabase.com`` the user ``postgres.<ref>`` selects the
project; a bare role name is refused with ``ENOIDENTIFIER (no tenant
identifier provided)``, which is how the RLS sweep tests failed there.
"""

from __future__ import annotations

import pytest

from sqlproof._dsn import conninfo_to_dict, with_user

POOLER = "aws-1-ca-central-1.pooler.supabase.com"


@pytest.mark.parametrize(
    "dsn",
    [
        f"postgresql://postgres.abcref:pw@{POOLER}:5432/postgres?sslmode=require",
        f"host={POOLER} port=6543 user=postgres.abcref dbname=postgres",
        f"host=10.0.0.1,{POOLER.upper()} user=postgres.abcref dbname=postgres",
    ],
)
def test_a_supavisor_dsn_keeps_the_tenant_suffix(dsn) -> None:
    params = conninfo_to_dict(with_user(dsn, "app_role"))
    assert params["user"] == "app_role.abcref"
    assert params["dbname"] == "postgres"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("host=127.0.0.1 user=postgres dbname=postgres", "app_role"),
        # A dot is legal in an ordinary role name: no rewriting off-pooler.
        ("host=db.example.com user=team.admin dbname=postgres", "app_role"),
        (f"host={POOLER} user=postgres dbname=postgres", "app_role"),
        ("host=evilpooler.supabase.com.example user=a.b dbname=postgres", "app_role"),
    ],
)
def test_other_hosts_get_the_role_name_unchanged(dsn, expected) -> None:
    assert conninfo_to_dict(with_user(dsn, "app_role"))["user"] == expected
