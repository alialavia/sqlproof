"""Scale follow-up 6: ranking `index_advisor` suggestions. No database:
the SQL-text helpers and the ranking are pure, and `run_index_advisor`'s
database calls are faked; the live suite runs the extension for real."""
from __future__ import annotations

import json
from typing import Any

import pytest

from sqlproof.scale import advisors
from sqlproof.scale.advisors import (
    NOT_AVAILABLE,
    NOT_INSTALLED,
    AdvisorHit,
    AdvisorRow,
    as_statement,
    index_relation,
    parameterize,
    rank_hits,
    run_index_advisor,
    split_statements,
)

# -- SQL text ---------------------------------------------------------------------


def test_split_statements_splits_on_top_level_semicolons_only():
    body = """ select 1; select 'a;b', "we;ird" from t -- c;
    ; select $q$ x; y $q$, $$;$$ /* ; */ ;  """
    assert split_statements(body) == [
        "select 1",
        "select 'a;b', \"we;ird\" from t -- c;",
        "select $q$ x; y $q$, $$;$$ /* ; */",
    ]


def test_split_statements_tolerates_an_unterminated_literal():
    assert split_statements("select 'oops; ") == ["select 'oops;"]
    assert split_statements("select $x$ never closed; ") == ["select $x$ never closed;"]


def test_parameterize_replaces_bare_parameter_references():
    text = "SELECT count(*) FROM invoices i WHERE customer_id = C AND i.c = 1 AND 'c' <> c"
    assert parameterize(text, {"c": "($1::bigint)"}) == (
        "SELECT count(*) FROM invoices i WHERE customer_id = ($1::bigint) "
        "AND i.c = 1 AND 'c' <> ($1::bigint)"
    )


def test_parameterize_leaves_qualifiers_calls_and_quoted_names_alone():
    text = 'SELECT c.id, c(1), "c", c . x FROM c -- c\nWHERE /* c */ x = c'
    assert parameterize(text, {"c": "$1"}) == (
        'SELECT c.id, c(1), "c", c . x FROM $1 -- c\nWHERE /* c */ x = $1'
    )
    assert parameterize(text, {}) == text


@pytest.mark.parametrize(
    ("text", "statement"),
    [
        ("select 1", "select 1"),
        ("WITH a AS (select 1) select * from a", "WITH a AS (select 1) select * from a"),
        ("(select count(*) from t)", "SELECT (select count(*) from t)"),
        ("n + 1", "SELECT n + 1"),
        ("selection > 0", "SELECT selection > 0"),
        ("update t set x = 1", "update t set x = 1"),
    ],
)
def test_as_statement_makes_plpgsql_expressions_plannable(text, statement):
    assert as_statement(text) == statement


@pytest.mark.parametrize(
    ("index_statement", "relation"),
    [
        ("CREATE INDEX ON public.invoices USING btree (customer_id)", ("public", "invoices")),
        ('CREATE INDEX ON "My Schema"."In""voices" USING btree (x)', ("My Schema", 'In"voices')),
        ("create index on only s.t using hash (x)", ("s", "t")),
        ("CREATE INDEX idx ON s.t (x)", ("s", "t")),
        ("CREATE INDEX ON invoices USING btree (x)", None),
        ("DROP INDEX x", None),
    ],
)
def test_index_relation_reads_index_advisors_statements(index_statement, relation):
    assert index_relation(index_statement) == relation


# -- ranking ----------------------------------------------------------------------


def _row(statement: str, before: float | None, after: float | None, *indexes: str) -> AdvisorRow:
    return AdvisorRow(statement, 0.0, 0.0, before, after, indexes, ())


INVOICES = "CREATE INDEX ON public.invoices USING btree (customer_id)"
CUSTOMERS = "CREATE INDEX ON public.customers USING btree (name)"
ELSEWHERE = "CREATE INDEX ON auth.users USING btree (email)"


def test_hits_rank_by_estimated_total_cost_saved_highest_first():
    rows = [
        _row("q-small", 100.0, 90.0, CUSTOMERS),
        _row("q-big", 1887.26, 271.72, INVOICES),
    ]
    hits, dropped = rank_hits(rows, [("public", "invoices"), ("public", "customers")])
    assert dropped == 0
    assert [(h.rank, h.index_statement, h.statement) for h in hits] == [
        (1, INVOICES, "q-big"),
        (2, CUSTOMERS, "q-small"),
    ]
    assert hits[0].relation == "public.invoices"
    assert hits[0].source == "index_advisor"
    assert hits[0].improvement == pytest.approx(1615.54)


def test_an_index_suggested_twice_keeps_the_statement_it_saves_most_on():
    rows = [_row("a", 50.0, 40.0, INVOICES), _row("b", 500.0, 40.0, INVOICES)]
    hits, _ = rank_hits(rows, [("public", "invoices")])
    assert [(h.rank, h.statement) for h in hits] == [(1, "b")]
    rows.reverse()
    assert [h.statement for h in rank_hits(rows, [("public", "invoices")])[0]] == ["b"]


def test_hits_on_relations_the_sweep_does_not_scale_are_dropped_and_counted():
    rows = [_row("q", 100.0, 10.0, INVOICES, ELSEWHERE, "not an index")]
    hits, dropped = rank_hits(rows, [("public", "invoices")])
    assert [h.index_statement for h in hits] == [INVOICES]
    assert dropped == 2


def test_rows_without_costs_yield_no_hits_and_ties_order_by_text():
    rows = [
        _row("failed", None, None, INVOICES),
        _row("q", 10.0, 5.0, INVOICES, CUSTOMERS),
    ]
    hits, _ = rank_hits(rows, [("public", "invoices"), ("public", "customers")])
    assert [h.index_statement for h in hits] == [CUSTOMERS, INVOICES]
    assert [h.rank for h in hits] == [1, 2]


# -- running it against a fake connection ----------------------------------------


class _Result:
    def __init__(self, row: Any) -> None:
        self.row = row

    def fetchone(self) -> Any:
        return self.row


class _Conn:
    """Answers the extension lookups by substring; raises on anything else."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.sql: list[str] = []

    def execute(self, sql: str, params: Any = None) -> _Result:
        self.sql.append(sql)
        for needle, answer in self.answers.items():
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                return _Result(answer)
        raise AssertionError(f"unexpected SQL: {sql}")


def test_an_extension_that_is_not_installed_is_a_note_not_a_failure():
    conn: Any = _Conn({"pg_extension": None, "pg_available_extensions": (1,)})
    assert run_index_advisor(
        conn, "fn", 0, nested_statements=None, relations=[]
    ) == ([], [NOT_INSTALLED])
    assert "CREATE EXTENSION index_advisor CASCADE" in NOT_INSTALLED
    # It never tries to create the extension.
    assert not any("CREATE" in sql.upper() for sql in conn.sql)


def test_an_extension_the_server_does_not_ship_says_so():
    conn: Any = _Conn({"pg_extension": None, "pg_available_extensions": None})
    assert run_index_advisor(conn, "fn", 0, nested_statements=None, relations=[]) == (
        [], [NOT_AVAILABLE],
    )


def test_any_database_error_becomes_a_note():
    conn: Any = _Conn({"pg_extension": RuntimeError("permission denied for pg_extension ")})
    hits, notes = run_index_advisor(conn, "fn", 0, nested_statements=None, relations=[])
    assert hits == []
    assert notes == ["index_advisor failed: permission denied for pg_extension"]


def test_run_index_advisor_parameterizes_ranks_and_explains(monkeypatch):
    asked: list[str] = []

    def fake_advise(conn, schema, statement, timeout_s):
        asked.append(statement)
        if "customer_id" in statement:
            return AdvisorRow(statement, 1.0, 0.5, 300.0, 100.0, (INVOICES, ELSEWHERE), ())
        if statement.startswith("SELECT n"):
            return AdvisorRow(statement, None, None, None, None, (), ("column n does not exist",))
        return AdvisorRow(statement, 0.0, 0.0, 0.26, 0.26, (), ())

    monkeypatch.setattr(advisors, "_extension_schema", lambda conn: "extensions")
    monkeypatch.setattr(advisors, "_signature", lambda conn, fn: (["bigint"], ["c"]))
    monkeypatch.setattr(advisors, "_advise", fake_advise)
    nested = {
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT app.count_for(5)",
        " select 1; select count(*) from invoices where customer_id = c ",
        "n + 1",
    }
    hits, notes = run_index_advisor(
        object(),  # type: ignore[arg-type]
        "app.count_for", 1,
        nested_statements=nested, relations=[("public", "invoices")], timeout_s=2.0,
    )
    assert asked == [
        "SELECT app.count_for($1::bigint)",
        "select 1",
        "select count(*) from invoices where customer_id = ($1::bigint)",
        "SELECT n + 1",
    ]
    assert [(h.rank, h.index_statement) for h in hits] == [(1, INVOICES)]
    assert notes == [
        "index_advisor could not plan 1 of 4 statement(s): 'SELECT n + 1': "
        "column n does not exist",
        "dropped 1 suggestion(s) on relations the sweep does not scale",
    ]


def test_without_nested_statements_only_the_outer_call_is_advised(monkeypatch):
    asked: list[str] = []

    def fake_advise(conn, schema, statement, timeout_s):
        asked.append(statement)
        return AdvisorRow(statement, 0.0, 0.0, 1.0, 1.0, (), ())

    monkeypatch.setattr(advisors, "_extension_schema", lambda conn: "public")
    # An overloaded function: no signature, so parameters stay untyped.
    monkeypatch.setattr(advisors, "_signature", lambda conn, fn: ([], []))
    monkeypatch.setattr(advisors, "_advise", fake_advise)
    hits, notes = run_index_advisor(
        object(), "fn", 2, nested_statements=None, relations=[]  # type: ignore[arg-type]
    )
    assert asked == ["SELECT fn($1, $2)"]
    assert hits == []
    assert notes == [advisors.OUTER_CALL_ONLY.format(function="fn")]
    assert "nested_plans=True" in notes[0]


# -- what the probe collects ------------------------------------------------------


def test_nested_plans_collect_the_text_of_statements_that_read_a_table():
    from sqlproof.scale.probe import _NestedPlans  # pyright: ignore[reportPrivateUsage]

    def notice(text: str, plan: dict[str, Any]) -> str:
        return "NOTICE:  duration: 0.010 ms  plan:\n" + json.dumps(
            {"Query Text": text, "Plan": plan}
        )

    scan = {"Node Type": "Aggregate", "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t"}]}
    statements: set[str] = set()
    nested = _NestedPlans(statements)
    assert nested.receive(notice("select count(*) from t", scan))
    assert nested.receive(notice("select count(*) from t", scan))
    assert nested.receive(notice("n + 1", {"Node Type": "Result"}))
    assert statements == {"select count(*) from t"}
    # Without a collector, nothing is kept (and nothing breaks).
    assert _NestedPlans().receive(notice("select count(*) from t", scan))


def test_an_advisor_hit_is_plain_data():
    hit = AdvisorHit(1, "public.t", "CREATE INDEX ON public.t USING btree (x)", "q",
                     1.0, 0.5, 10.0, 4.0)
    assert hit.improvement == 6.0
