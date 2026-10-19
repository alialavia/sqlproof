"""A foreign-key column takes its parent's keys, unfiltered by its own CHECKs."""

from __future__ import annotations

import random
import warnings

import pytest
from hypothesis import find, settings
from hypothesis import strategies as st

from sqlproof.exceptions import UnhonoredCheckWarning
from sqlproof.generators.bulk import bulk_table_rows
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql


def _child(check: str):
    schema = parse_schema_sql(
        "CREATE TABLE p (id int PRIMARY KEY);"
        "CREATE TABLE c (id int PRIMARY KEY, note text,"
        f" p_id int NOT NULL REFERENCES p(id){check});"
    )
    return schema.table("c")


def _generate(table, path: str, overrides=None) -> list[warnings.WarningMessage]:
    parents = [{"id": value} for value in range(1, 4)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if path == "bulk":
            list(bulk_table_rows(
                table, count=3, rng=random.Random(1), parent_counts={"p": 3},
                columns=overrides,
            ))
        else:
            find(
                table_rows_strategy(
                    table, count=3, parent_rows={"p": parents}, columns=overrides
                ),
                lambda _rows: True,
                settings=settings(database=None, deadline=None),
            )
    return [w for w in caught if issubclass(w.category, UnhonoredCheckWarning)]


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_a_check_on_a_foreign_key_column_is_reported(path):
    """Contract: PostgreSQL rejects parent keys 1..3 under CHECK (p_id > 100);
    since sqlproof does not apply it, it must say so."""
    found = _generate(_child(" CHECK (p_id > 100)"), path)
    assert found and "p_id" in str(found[0].message)


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_checks_that_leave_foreign_keys_alone_are_not_reported(path):
    """The warning is about foreign-key columns only: a CHECK on another
    column that sqlproof honors stays quiet."""
    assert _generate(_child(", CHECK (length(note) < 5)"), path) == []


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_an_override_for_the_foreign_key_column_silences_the_warning(path):
    """An override supplies the column's values, so the parent's keys and
    the CHECK no longer meet."""
    overrides = {"c.p_id": st.just(1) if path == "hypothesis" else (lambda _ctx: 1)}
    assert _generate(_child(" CHECK (p_id > 100)"), path, overrides) == []


@pytest.mark.parametrize(
    "check",
    [" CHECK (mod(p_id, 2) = 0)", " CHECK ((p_id > 100 AND p_id < 200) OR p_id IS NULL)"],
    ids=["unrecognized", "range-or-null"],
)
def test_checks_sqlproof_cannot_or_can_only_partly_read_are_reported_too(check):
    """Whether a CHECK on the foreign key is a function call or a compound
    of ranges, the parent's keys are still unfiltered by it."""
    found = _generate(_child(check), "bulk")
    assert found and "p_id" in str(found[0].message)
