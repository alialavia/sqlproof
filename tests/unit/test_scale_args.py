"""Argument resolvers.

A resolver is re-run against the freshly loaded data at every scale
point, which is not a refinement but a requirement: the dataset is
regenerated at each factor and keys are assigned deterministically
(`_unique_value` gives id = i + 1), so a literal that exists at 8x may
not exist at 1x.
"""
from __future__ import annotations

from sqlproof.scale.args import heaviest, median_key, random_key, resolve_args


class FakeConn:
    """Records SQL and returns a canned scalar. Enough to test that a
    resolver builds the query it claims to; the real query is exercised
    in the integration tests."""

    def __init__(self, value):
        self.value = value
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return self

    def fetchone(self):
        return (self.value,)


def test_literals_pass_through_untouched():
    conn = FakeConn(None)
    assert resolve_args(conn, [42, "abc", None]) == (42, "abc", None)
    assert conn.sql == []


def test_resolver_is_called_with_the_connection():
    conn = FakeConn(7)
    assert resolve_args(conn, [heaviest("customers.id")]) == (7,)
    assert len(conn.sql) == 1


def test_heaviest_orders_by_descending_child_count():
    conn = FakeConn(3)
    heaviest("customers.id")(conn)
    sql = conn.sql[0].lower()
    assert "order by" in sql
    assert "desc" in sql
    assert "limit 1" in sql


def test_median_key_uses_an_offset_rather_than_ordering_by_count():
    conn = FakeConn(5)
    median_key("customers.id")(conn)
    sql = conn.sql[0].lower()
    assert "offset" in sql


def test_random_key_is_deterministic_for_a_given_seed():
    a = FakeConn(1)
    b = FakeConn(1)
    random_key("customers.id", seed=99)(a)
    random_key("customers.id", seed=99)(b)
    assert a.sql == b.sql


def test_mixed_literals_and_resolvers_keep_their_positions():
    conn = FakeConn(9)
    assert resolve_args(conn, ["first", heaviest("t.id"), 3]) == ("first", 9, 3)


def test_a_plain_callable_is_treated_as_a_resolver():
    conn = FakeConn(None)
    assert resolve_args(conn, [lambda c: 123]) == (123,)
