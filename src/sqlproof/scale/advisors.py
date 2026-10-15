"""Rank Supabase `index_advisor` suggestions for a measured function.

Scale follow-up 6. A scale result says "this grows linearly"; an advisor
hit says "add an index on `invoices.customer_id`". The umbrella spec
rules out reimplementing Supabase's advisors, so this only calls
`index_advisor` (which needs `hypopg`) and ranks what it returns.

It runs once, after the ladder, on the largest factor's data -- the
modelled tables are full then and freshly ANALYZEd, so the planner's cost
estimates reflect the largest measured size -- and before the sweep
empties them. `index_advisor` only plans (EXPLAIN with hypothetical
indexes, which are backend-local); it never creates an index or runs the
statements.

Which statements it sees:

- With `nested_plans`, every statement the function ran that read a
  table, as auto_explain reported its "Query Text". Parameter names in
  that text (`customer_id = c`) become typed generic parameters
  (`($1::bigint)`), which `index_advisor` plans as a generic plan.
- Always, the outer call `SELECT fn($1, ...)`. Without `nested_plans`
  that is all it sees, and `index_advisor` sees through a function only
  when Postgres inlines it (a single-SELECT SQL function without
  aggregates, among other rules) -- say so in the notes.

Never fails the measurement: an extension that is not installed, a
statement `index_advisor` cannot plan, or any other error becomes a note.
Never creates the extension either: that is the user's decision for
their database (`CREATE EXTENSION index_advisor CASCADE`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlproof import _database as database

SOURCE = "index_advisor"

NOT_INSTALLED = "index_advisor not installed (CREATE EXTENSION index_advisor CASCADE)"
NOT_AVAILABLE = (
    "index_advisor is not available on this server: it ships with the Supabase "
    "Postgres images (https://github.com/supabase/index_advisor); install it and "
    "then CREATE EXTENSION index_advisor CASCADE"
)
OUTER_CALL_ONLY = (
    "nested_plans is off, so index_advisor saw only the outer SELECT {function}(...) "
    "call; it sees inside a function only when Postgres inlines it. Pass "
    "nested_plans=True to advise on the statements the function runs."
)


@dataclass(frozen=True, slots=True)
class AdvisorHit:
    """One suggested index, ranked by the estimated cost it saves.

    The costs are the planner's estimates for `statement` -- the one the
    suggestion came from -- before and after ALL the indexes
    `index_advisor` suggested for it, at the largest measured data size.
    """

    rank: int
    relation: str
    index_statement: str
    statement: str
    startup_cost_before: float
    startup_cost_after: float
    total_cost_before: float
    total_cost_after: float
    source: str = SOURCE

    @property
    def improvement(self) -> float:
        return self.total_cost_before - self.total_cost_after


@dataclass(frozen=True, slots=True)
class AdvisorRow:
    """One `index_advisor(statement)` result row."""

    statement: str
    startup_cost_before: float | None
    startup_cost_after: float | None
    total_cost_before: float | None
    total_cost_after: float | None
    index_statements: Sequence[str]
    errors: Sequence[str]


# -- SQL text ------------------------------------------------------------------

# Literals and comments, which no rewrite below may touch, and the tokens
# between them. Dollar quotes match their own tag.
_TOKEN = re.compile(
    r"""
    (?P<skip>
        '(?:[^']|'')*'?                        # string literal
      | "(?:[^"]|"")*"?                        # quoted identifier
      | \$(?P<tag>[A-Za-z_][A-Za-z_0-9]*)?\$   # dollar quote (body handled below)
      | --[^\n]*                               # line comment
      | /\*.*?(?:\*/|\Z)                       # block comment
    )
  | (?P<word>[A-Za-z_][A-Za-z_0-9$]*)
  | (?P<semi>;)
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def _tokens(text: str) -> Iterable[tuple[str, str]]:
    """(kind, text) pairs covering `text` exactly: skip, word, semi, other."""
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        assert match is not None  # `other` matches any single character
        kind = cast(str, match.lastgroup)
        end = match.end()
        if kind == "skip" and match.group(0).startswith("$"):
            delimiter = match.group(0)
            close = text.find(delimiter, end)
            end = len(text) if close < 0 else close + len(delimiter)
        yield kind, text[pos:end]
        pos = end


def split_statements(text: str) -> list[str]:
    """`text` split on top-level semicolons, each part stripped; empty
    parts dropped. A SQL function's whole body is one auto_explain
    "Query Text", and `index_advisor` refuses a semicolon."""
    parts: list[str] = []
    current: list[str] = []
    for kind, token in _tokens(text):
        if kind == "semi":
            parts.append("".join(current))
            current = []
        else:
            current.append(token)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parameterize(text: str, parameters: Mapping[str, str]) -> str:
    """Replace each bare reference to a function parameter with its
    generic parameter, e.g. `c` -> `($1::bigint)`.

    `parameters` maps lower-cased parameter names to their replacement.
    A word is a reference when it is unquoted, outside literals and
    comments, not qualified (`t.c`) and not a call (`c(`). Inside a SQL
    function a column of the same name would win over the parameter;
    the text cannot tell them apart, so such a statement is advised as
    if the parameter were meant.
    """
    if not parameters:
        return text
    tokens = list(_tokens(text))
    out: list[str] = []
    for index, (kind, token) in enumerate(tokens):
        replacement = parameters.get(token.lower()) if kind == "word" else None
        if replacement is not None and not _qualified(tokens, index):
            out.append(replacement)
        else:
            out.append(token)
    return "".join(out)


def _qualified(tokens: Sequence[tuple[str, str]], index: int) -> bool:
    before = next((t for _k, t in reversed(tokens[:index]) if not t.isspace()), "")
    after = next((t for _k, t in tokens[index + 1 :] if not t.isspace()), "")
    return before == "." or after in {".", "("}


_STATEMENT_KEYWORDS = frozenset(
    {"select", "with", "insert", "update", "delete", "merge", "values", "table"}
)


def as_statement(text: str) -> str:
    """PL/pgSQL reports an expression it evaluates (`RETURN (SELECT ...)`)
    as the bare expression; make it a SELECT so it can be planned."""
    first = next((t for k, t in _tokens(text) if k == "word"), "")
    starts_with_word = text.lstrip()[:1].isalpha()
    if starts_with_word and first.lower() in _STATEMENT_KEYWORDS:
        return text
    return f"SELECT {text}"


# -- ranking ---------------------------------------------------------------------

_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[^\s."(]+)'
_INDEX_RELATION = re.compile(
    rf"^\s*CREATE\s+INDEX\s+(?:\S+\s+)?ON\s+(?:ONLY\s+)?({_IDENTIFIER})\.({_IDENTIFIER})",
    re.IGNORECASE,
)


def _unquote(identifier: str) -> str:
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1].replace('""', '"')
    return identifier


def index_relation(index_statement: str) -> tuple[str, str] | None:
    """(schema, table) of a `CREATE INDEX ON schema.table ...` statement,
    as `index_advisor` writes them, or None when it is not one."""
    match = _INDEX_RELATION.match(index_statement)
    if match is None:
        return None
    return _unquote(match.group(1)), _unquote(match.group(2))


def rank_hits(
    rows: Iterable[AdvisorRow], relations: Iterable[tuple[str, str]]
) -> tuple[list[AdvisorHit], int]:
    """Every suggested index on one of `relations`, highest estimated
    total-cost saving first, ranked from 1; and how many suggestions were
    dropped for naming another relation.

    An index suggested for several statements is kept once, with the
    statement it saves most on. Ties fall back to the index statement's
    text, so the order is deterministic.
    """
    touched = set(relations)
    best: dict[str, AdvisorHit] = {}
    dropped = 0
    for row in rows:
        if row.total_cost_before is None or row.total_cost_after is None:
            continue
        for index_statement in row.index_statements:
            relation = index_relation(index_statement)
            if relation is None or relation not in touched:
                dropped += 1
                continue
            hit = AdvisorHit(
                rank=0,
                relation=".".join(relation),
                index_statement=index_statement,
                statement=row.statement,
                startup_cost_before=row.startup_cost_before or 0.0,
                startup_cost_after=row.startup_cost_after or 0.0,
                total_cost_before=row.total_cost_before,
                total_cost_after=row.total_cost_after,
            )
            kept = best.get(index_statement)
            if kept is None or hit.improvement > kept.improvement:
                best[index_statement] = hit
    ordered = sorted(best.values(), key=lambda h: (-h.improvement, h.index_statement))
    hits = [
        AdvisorHit(
            rank=rank,
            relation=hit.relation,
            index_statement=hit.index_statement,
            statement=hit.statement,
            startup_cost_before=hit.startup_cost_before,
            startup_cost_after=hit.startup_cost_after,
            total_cost_before=hit.total_cost_before,
            total_cost_after=hit.total_cost_after,
        )
        for rank, hit in enumerate(ordered, start=1)
    ]
    return hits, dropped


# -- running it ------------------------------------------------------------------


def run_index_advisor(
    conn: database.Connection,
    function: str,
    arg_count: int,
    *,
    nested_statements: Iterable[str] | None,
    relations: Sequence[tuple[str, str]],
    timeout_s: float | None = None,
) -> tuple[list[AdvisorHit], list[str]]:
    """Ask `index_advisor` about the statements `function` runs, and rank
    the answers (`rank_hits`). Returns (hits, notes); never raises for
    anything the database says -- each problem becomes a note.

    `nested_statements` are auto_explain's query texts (None when
    nested_plans was off). `relations` are the tables the sweep scaled:
    a suggestion on any other relation is dropped, since its cost
    estimate does not reflect measured data.
    """
    notes: list[str] = []
    try:
        schema = _extension_schema(conn)
        if schema is None:
            return [], [_missing_reason(conn)]
        statements = _statements(conn, function, arg_count, nested_statements)
        if nested_statements is None:
            notes.append(OUTER_CALL_ONLY.format(function=function))
        rows: list[AdvisorRow] = []
        failed: list[str] = []
        for statement in statements:
            row = _advise(conn, schema, statement, timeout_s)
            rows.append(row)
            if row.errors:
                failed.append(f"{statement!r}: {'; '.join(row.errors)}")
        hits, dropped = rank_hits(rows, relations)
    except Exception as exc:  # never fail the measurement over an advisor
        return [], [*notes, f"index_advisor failed: {str(exc).strip()}"]
    if failed:
        notes.append(
            f"index_advisor could not plan {len(failed)} of {len(statements)} "
            f"statement(s): {failed[0]}"
        )
    if dropped:
        notes.append(
            f"dropped {dropped} suggestion(s) on relations the sweep does not scale"
        )
    return hits, notes


def _extension_schema(conn: database.Connection) -> str | None:
    row = conn.execute(
        "SELECT quote_ident(n.nspname) FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'index_advisor'"
    ).fetchone()
    return None if row is None else str(_first(row))


def _missing_reason(conn: database.Connection) -> str:
    row = conn.execute(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'index_advisor'"
    ).fetchone()
    return NOT_INSTALLED if row is not None else NOT_AVAILABLE


def _statements(
    conn: database.Connection,
    function: str,
    arg_count: int,
    nested_statements: Iterable[str] | None,
) -> list[str]:
    """The outer call, then each distinct nested statement, parameterized."""
    types, names = _signature(conn, function)
    casts = [
        f"${index + 1}" if index >= len(types) else f"${index + 1}::{types[index]}"
        for index in range(arg_count)
    ]
    outer = f"SELECT {function}({', '.join(casts)})"
    parameters = {
        name.lower(): f"({casts[index]})"
        for index, name in enumerate(names[:arg_count])
        if name
    }
    statements = [outer]
    for text in sorted(set(nested_statements or ())):
        for part in split_statements(text):
            if part.lstrip()[:7].upper() == "EXPLAIN":
                continue  # the probe's own top-level statement
            statement = as_statement(parameterize(part, parameters))
            if statement not in statements:
                statements.append(statement)
    return statements


def _signature(conn: database.Connection, function: str) -> tuple[list[str], list[str]]:
    """Input argument types and names, in order; empty when `function` is
    overloaded (or unknown), so its parameters are left untyped."""
    row = conn.execute(
        "SELECT ARRAY(SELECT format_type(t, NULL) FROM unnest(p.proargtypes) AS t), "
        "COALESCE(ARRAY(SELECT a.name FROM unnest(p.proargnames, "
        "COALESCE(p.proargmodes, array_fill('i'::\"char\", "
        "ARRAY[cardinality(p.proargnames)]))) AS a(name, mode) "
        "WHERE a.mode IN ('i', 'b', 'v')), '{}') "
        "FROM pg_proc p WHERE p.oid = to_regproc(%s)",
        [function],
    ).fetchone()
    if row is None:
        return [], []
    values: Any = cast(Any, row).values() if isinstance(row, dict) else row
    types, names = list(values)
    return [str(t) for t in types or ()], [str(n or "") for n in names or ()]


def _advise(
    conn: database.Connection, schema: str, statement: str, timeout_s: float | None
) -> AdvisorRow:
    """One `index_advisor` call, in a transaction or savepoint that is
    always rolled back: hypothetical indexes are backend-local, but the
    statement timeout and anything else it sets must not outlive it."""
    opened = not conn.in_transaction
    with conn.cursor() as cur:
        if opened:
            cur.execute("BEGIN")
        cur.execute("SAVEPOINT sqlproof_advisor")
        try:
            if timeout_s is not None:
                cur.execute(f"SET LOCAL statement_timeout = {max(1, round(timeout_s * 1000))}")
            # index_advisor calls hypopg's functions unqualified: append the
            # schemas of both extensions to the search path, after what is
            # there, so the statements still resolve their names as before.
            cur.execute(
                "SELECT set_config('search_path', concat_ws(', ', "
                "nullif(current_setting('search_path'), ''), "
                "(SELECT string_agg(quote_ident(n.nspname), ', ') FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace "
                "WHERE e.extname IN ('hypopg', 'index_advisor'))), true)"
            )
            try:
                cur.execute(
                    "SELECT startup_cost_before::text, startup_cost_after::text, "
                    "total_cost_before::text, total_cost_after::text, "
                    f"index_statements, errors FROM {schema}.index_advisor(%s)",
                    [statement],
                )
                row: Any = cur.fetchone()
            except Exception as exc:
                return AdvisorRow(statement, None, None, None, None, (), (str(exc).strip(),))
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT sqlproof_advisor")
            cur.execute("RELEASE SAVEPOINT sqlproof_advisor")
            if opened:
                cur.execute("ROLLBACK")
    values: Any = cast(Any, row).values() if isinstance(row, dict) else row
    s_before, s_after, t_before, t_after, indexes, errors = list(values)
    return AdvisorRow(
        statement,
        _cost(s_before),
        _cost(s_after),
        _cost(t_before),
        _cost(t_after),
        tuple(str(i) for i in indexes or ()),
        tuple(str(e) for e in errors or ()),
    )


def _cost(value: Any) -> float | None:
    if value is None or value == "null":
        return None
    return float(value)


def _first(row: Any) -> Any:
    values: Any = cast(Any, row).values() if isinstance(row, dict) else row
    return next(iter(values))
