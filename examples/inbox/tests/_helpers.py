"""Shared test helpers for the inbox sample.

Currently exports `vector_strategy(dim)` — a workaround for SqlProof
issue #69 (the schema parser doesn't yet recognise `vector(N)` columns,
so we override embedding columns with a strategy that emits Postgres
vector-literal strings of the right dimension).
"""

from __future__ import annotations

from hypothesis import strategies as st


def vector_strategy(dim: int) -> st.SearchStrategy[str]:
    """Generate a Postgres vector literal of the given dimension.

    Returns strings shaped like ``'[0.123,-0.456,...]'`` that PostgreSQL's
    `vector` type accepts directly. Components are bounded to a small
    range so generated vectors stay reasonable for cosine/L2 distance
    computations during property tests.
    """
    component = st.floats(
        min_value=-1.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    )
    return st.lists(component, min_size=dim, max_size=dim).map(
        lambda xs: "[" + ",".join(f"{x:.6f}" for x in xs) + "]",
    )
