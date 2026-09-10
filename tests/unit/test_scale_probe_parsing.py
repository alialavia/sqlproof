"""Parsing an EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) tree.

Pure: these are dicts shaped like Postgres output, no database. The
per-loop trap in `test_nested_loop_work_multiplies_by_loops` is the
reason this parsing gets its own task -- reading `Actual Rows` without
multiplying by `Actual Loops` is exactly where a quadratic hides.
"""
from __future__ import annotations

from sqlproof.scale.probe import parse_plan


def _plan(**node):
    """Wrap a node the way Postgres wraps a plan: a one-element list
    holding a dict with a "Plan" key."""
    base = {
        "Node Type": "Seq Scan",
        "Shared Hit Blocks": 0,
        "Shared Read Blocks": 0,
        "Actual Loops": 1,
    }
    base.update(node)
    return [{"Plan": base, "Execution Time": 1.5}]


def test_work_sums_hit_and_read_blocks():
    work, _mem, _temp, _h, _ms = parse_plan(
        _plan(**{"Shared Hit Blocks": 30, "Shared Read Blocks": 12})
    )
    assert work == 42


def test_work_is_the_root_total_not_a_sum_over_the_tree():
    """Postgres's buffer counts are INCLUSIVE of children: the root's
    figure already contains every descendant's. Verified on a live
    nested loop -- Aggregate=24, Nested Loop=24, children 23 + 1.
    Summing the tree would count the same pages several times."""
    work, _m, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Shared Hit Blocks": 24,
                "Plans": [
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 23,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                    {"Node Type": "Materialize", "Shared Hit Blocks": 1,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                ],
            }
        )
    )
    assert work == 24


def test_work_does_not_multiply_by_loops():
    """The single most dangerous mistake available here.

    `Actual Rows` and `Actual Time` are per-loop AVERAGES, but buffer
    counts are cumulative TOTALS. Verified live: a Materialize node with
    loops=5000 reports sharedHit=1, not 5000.

    Multiplying would inflate by a factor that GROWS WITH n (loops grow
    with the data), fabricating superlinear growth from a linear
    function and reporting a false quadratic. Every exponent the feature
    produced would be wrong, in the direction that invents problems."""
    work, _m, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Node Type": "Nested Loop",
                "Shared Hit Blocks": 24,
                "Plans": [
                    {"Node Type": "Materialize", "Shared Hit Blocks": 1,
                     "Shared Read Blocks": 0, "Actual Loops": 5000,
                     "Actual Rows": 20},
                ],
            }
        )
    )
    assert work == 24  # NOT 24 + 1*5000


def test_peak_memory_takes_the_largest_node_not_the_sum():
    _w, mem, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Node Type": "Sort",
                "Sort Space Used": 512,
                "Plans": [
                    {"Node Type": "Hash", "Peak Memory Usage": 2048,
                     "Shared Hit Blocks": 0, "Shared Read Blocks": 0,
                     "Actual Loops": 1},
                ],
            }
        )
    )
    assert mem == 2048


def test_temp_blocks_signal_a_spill():
    _w, _m, temp, _h, _ms = parse_plan(
        _plan(**{"Temp Read Blocks": 40, "Temp Written Blocks": 60})
    )
    assert temp == 100


def test_plan_hash_ignores_row_counts_and_costs():
    a = parse_plan(_plan(**{"Actual Rows": 10, "Total Cost": 1.0}))[3]
    b = parse_plan(_plan(**{"Actual Rows": 999999, "Total Cost": 5000.0}))[3]
    assert a == b


def test_plan_hash_changes_when_node_types_change():
    a = parse_plan(_plan(**{"Node Type": "Seq Scan"}))[3]
    b = parse_plan(_plan(**{"Node Type": "Index Scan"}))[3]
    assert a != b


def test_plan_hash_changes_when_nesting_changes():
    flat = parse_plan(_plan(**{"Node Type": "Hash Join"}))[3]
    nested = parse_plan(
        _plan(
            **{
                "Node Type": "Hash Join",
                "Plans": [
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 0,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                ],
            }
        )
    )[3]
    assert flat != nested


def test_execution_time_is_read_from_the_envelope():
    _w, _m, _t, _h, ms = parse_plan(_plan())
    assert ms == 1.5
