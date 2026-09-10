"""Scale measurement: is this function's growth curve acceptable?

The output is an assertion, not a report -- CI goes red when someone
writes a query that will not survive growth, the way
`assert_no_survivors()` works for mutation testing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import psycopg

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale.args import heaviest, median_key, random_key
from sqlproof.scale.result import ScaleResult
from sqlproof.scale.sweep import run_sweep

if TYPE_CHECKING:
    from sqlproof.core import SqlProof

__all__ = [
    "ScaleResult",
    "heaviest",
    "median_key",
    "random_key",
    "scale_analysis",
]


def scale_analysis(
    proof: SqlProof,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    **kwargs: Any,
) -> ScaleResult:
    """Measure how `function` scales, using `proof`'s schema and database.

    Opens its own autocommit connection: the sweep issues TRUNCATE,
    COPY and ANALYZE, which want a lifecycle of their own rather than
    sharing the property runner's transaction.
    """
    dsn = proof.config.connection_string
    if dsn is None:
        msg = (
            "scale_analysis needs a real database: construct SqlProof with "
            "a connection_string rather than a schema file."
        )
        raise SqlProofUsageError(msg)
    with psycopg.connect(dsn, autocommit=True) as conn:
        return run_sweep(conn, proof.schema_info, function, sizes=sizes, args=args, **kwargs)
