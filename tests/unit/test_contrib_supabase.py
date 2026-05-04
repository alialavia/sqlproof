from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sqlproof.contrib.supabase import (
    as_supabase_user,
    seed_supabase_test_users,
    seed_test_users_directly,
)


class FakeClaimsClient:
    """Minimal SqlProofClient stand-in that simulates `set_config(...,..., true)`
    behavior by tracking a single transaction-local GUC value."""

    def __init__(self, initial: str | None = None) -> None:
        self.guc: str | None = initial
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, *params: Any) -> int:
        self.calls.append((sql, params))
        if "set_config" in sql:
            _, value = params[0], params[1]
            self.guc = value if value != "" else None
        return 0

    def scalar(self, sql: str, *params: Any) -> Any:
        self.calls.append((sql, params))
        if "current_setting" in sql:
            return self.guc
        return None


def test_as_supabase_user_sets_jwt_claims_inside_block() -> None:
    db = FakeClaimsClient()
    with as_supabase_user(db, "user-1"):
        assert db.guc is not None
        claims = json.loads(db.guc)
        assert claims == {"sub": "user-1", "role": "authenticated"}


def test_as_supabase_user_restores_prior_value_on_exit() -> None:
    db = FakeClaimsClient(initial='{"sub":"prior","role":"authenticated"}')
    with as_supabase_user(db, "user-1"):
        assert json.loads(db.guc)["sub"] == "user-1"
    assert db.guc == '{"sub":"prior","role":"authenticated"}'


def test_as_supabase_user_clears_guc_when_no_prior_value() -> None:
    db = FakeClaimsClient()
    with as_supabase_user(db, "user-1"):
        pass
    assert db.guc is None


def test_as_supabase_user_nested_blocks_stack_and_unwind() -> None:
    db = FakeClaimsClient()
    with as_supabase_user(db, "outer"):
        assert json.loads(db.guc)["sub"] == "outer"
        with as_supabase_user(db, "inner", role="anon"):
            inner = json.loads(db.guc)
            assert inner == {"sub": "inner", "role": "anon"}
        assert json.loads(db.guc)["sub"] == "outer"
    assert db.guc is None


def test_as_supabase_user_restores_guc_after_exception() -> None:
    db = FakeClaimsClient(initial='{"sub":"prior","role":"authenticated"}')
    with pytest.raises(RuntimeError):
        with as_supabase_user(db, "user-1"):
            raise RuntimeError("boom")
    assert db.guc == '{"sub":"prior","role":"authenticated"}'


def test_as_supabase_user_merges_extra_claims() -> None:
    db = FakeClaimsClient()
    with as_supabase_user(db, "user-1", extra_claims={"app_metadata": {"plan": "pro"}}):
        claims = json.loads(db.guc)
        assert claims == {
            "sub": "user-1",
            "role": "authenticated",
            "app_metadata": {"plan": "pro"},
        }


def test_as_supabase_user_extra_claims_can_override_role() -> None:
    db = FakeClaimsClient()
    with as_supabase_user(db, "user-1", extra_claims={"role": "service_role"}):
        claims = json.loads(db.guc)
        assert claims["role"] == "service_role"


class FakeAuthUsersClient:
    """SqlProofClient stand-in that simulates `auth.users` writes for direct seeding."""

    def __init__(self, prepopulated: list[dict[str, str]] | None = None) -> None:
        self.users: list[dict[str, str]] = list(prepopulated or [])
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, *params: Any) -> int:
        self.executes.append((sql, params))
        if "INSERT INTO auth.users" in sql:
            email = params[0]
            if any(user["email"] == email for user in self.users):
                return 0
            self.users.append({"id": f"user-{len(self.users)}", "email": email})
            return 1
        return 0

    def __getattr__(self, name: str):  # pragma: no cover
        raise AttributeError(name)

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        if "FROM auth.users" in sql and "LIKE" in sql:
            pattern = params[0]
            prefix = pattern.replace(r"\_", "_").rstrip("%@test.invalid").rstrip("%")
            matched = [u for u in self.users if u["email"].startswith(prefix)]
            matched.sort(key=lambda u: u["email"])
            return [{"id": u["id"]} for u in matched]
        return []


def test_seed_test_users_directly_inserts_requested_count() -> None:
    db = FakeAuthUsersClient()
    ids = seed_test_users_directly(db, count=3)
    assert len(ids) == 3
    assert {u["email"] for u in db.users} == {
        "sqlproof_0@test.invalid",
        "sqlproof_1@test.invalid",
        "sqlproof_2@test.invalid",
    }


def test_seed_test_users_directly_is_idempotent() -> None:
    db = FakeAuthUsersClient()
    first_ids = seed_test_users_directly(db, count=2)
    second_ids = seed_test_users_directly(db, count=2)
    assert first_ids == second_ids
    assert len(db.users) == 2


def test_seed_test_users_directly_returns_existing_users() -> None:
    db = FakeAuthUsersClient(
        prepopulated=[
            {"id": "preexisting-0", "email": "sqlproof_0@test.invalid"},
        ]
    )
    ids = seed_test_users_directly(db, count=2)
    assert "preexisting-0" in ids
    assert len(ids) == 2


def test_seed_test_users_directly_rejects_negative_count() -> None:
    db = FakeAuthUsersClient()
    with pytest.raises(ValueError, match="non-negative"):
        seed_test_users_directly(db, count=-1)


def test_seed_test_users_directly_with_custom_prefix_and_domain() -> None:
    db = FakeAuthUsersClient()
    seed_test_users_directly(db, count=2, email_prefix="probe_", email_domain="example.test")
    assert {u["email"] for u in db.users} == {
        "probe_0@example.test",
        "probe_1@example.test",
    }


def test_seed_supabase_test_users_replaces_test_invalid_users(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None = None) -> None:
            self._payload = payload or {}

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            calls.append(("raise_for_status", "", None))

    class FakeClient:
        def __init__(
            self,
            *,
            base_url: str,
            headers: dict[str, str],
            timeout: float,
        ) -> None:
            calls.append(("client", base_url, headers))
            calls.append(("timeout", str(timeout), None))

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            calls.append(("close", "", None))

        def get(self, path: str) -> FakeResponse:
            calls.append(("get", path, None))
            return FakeResponse(
                {
                    "users": [
                        {"id": "delete-me", "email": "sqlproof_0@test.invalid"},
                        {"id": "keep-me", "email": "person@example.com"},
                    ]
                }
            )

        def delete(self, path: str) -> FakeResponse:
            calls.append(("delete", path, None))
            return FakeResponse()

        def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
            calls.append(("post", path, json))
            return FakeResponse()

    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setitem(
        sys.modules,
        "httpx",
        SimpleNamespace(Client=FakeClient),
    )

    seed_supabase_test_users(db=object(), count=2)

    assert (
        "client",
        "http://localhost:54321",
        {"Authorization": "Bearer service-role", "apikey": "service-role"},
    ) in calls
    assert ("timeout", "5.0", None) in calls
    assert ("delete", "/auth/v1/admin/users/delete-me", None) not in calls
    assert ("delete", "/auth/v1/admin/users/keep-me", None) not in calls
    assert (
        "post",
        "/auth/v1/admin/users",
        {
            "email": "sqlproof_1@test.invalid",
            "password": "test_password",
            "email_confirm": True,
        },
    ) in calls


def test_seed_supabase_test_users_reuses_existing_test_users(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object] | None = None) -> None:
            self._payload = payload or {}

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def get(self, path: str) -> FakeResponse:
            calls.append(("get", path, None))
            return FakeResponse(
                {
                    "users": [
                        {"id": "existing-0", "email": "sqlproof_0@test.invalid"},
                        {"id": "existing-1", "email": "sqlproof_1@test.invalid"},
                    ]
                }
            )

        def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
            calls.append(("post", path, json))
            return FakeResponse()

    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=FakeClient))

    seed_supabase_test_users(db=object(), count=2)

    assert calls == [("get", "/auth/v1/admin/users", None)]
