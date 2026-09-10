"""Parsing an EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) tree.

Pure: these are dicts shaped like Postgres output, no database. The
per-loop trap in `test_nested_loop_work_multiplies_by_loops` is the
reason this parsing gets its own task -- reading `Actual Rows` without
multiplying by `Actual Loops` is exactly where a quadratic hides.
"""
from __future__ import annotations

import json

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


# --- CTE and subplan trees, captured live (Ruling AU) ---
#
# Real `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output from the
# sqlproof-pg container (Postgres 15), pasted verbatim -- the options the
# probe itself uses. In a JSON plan an InitPlan or SubPlan is simply
# another entry in its parent's "Plans", tagged by "Parent Relationship".
#
# CTE_EXPLAIN_JSON, over t = 2,000 md5 payloads:
#   WITH s AS MATERIALIZED (SELECT payload FROM t ORDER BY payload)
#   SELECT count(*) FROM s
# SUBPLAN_EXPLAIN_JSON, over o = 20 rows and u, w = 500 rows each, with
# enable_nestloop and enable_mergejoin off so the correlated subquery
# hashes:
#   SELECT count(*) FROM o WHERE o.k < (SELECT count(*) FROM u JOIN w
#   ON w.x = u.v WHERE u.id > o.id * 20)

CTE_EXPLAIN_JSON = """
[
  {
    "Plan": {
      "Node Type": "Aggregate",
      "Strategy": "Plain",
      "Partial Mode": "Simple",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 198.66,
      "Total Cost": 198.67,
      "Plan Rows": 1,
      "Plan Width": 8,
      "Actual Startup Time": 1.247,
      "Actual Total Time": 1.247,
      "Actual Rows": 1,
      "Actual Loops": 1,
      "Shared Hit Blocks": 19,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Sort",
          "Parent Relationship": "InitPlan",
          "Subplan Name": "CTE s",
          "Parallel Aware": false,
          "Async Capable": false,
          "Startup Cost": 148.66,
          "Total Cost": 153.66,
          "Plan Rows": 2000,
          "Plan Width": 33,
          "Actual Startup Time": 0.949,
          "Actual Total Time": 1.018,
          "Actual Rows": 2000,
          "Actual Loops": 1,
          "Sort Key": ["t.payload"],
          "Sort Method": "quicksort",
          "Sort Space Used": 205,
          "Sort Space Type": "Memory",
          "Shared Hit Blocks": 19,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Seq Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": false,
              "Async Capable": false,
              "Relation Name": "t",
              "Alias": "t",
              "Startup Cost": 0.00,
              "Total Cost": 39.00,
              "Plan Rows": 2000,
              "Plan Width": 33,
              "Actual Startup Time": 0.004,
              "Actual Total Time": 0.104,
              "Actual Rows": 2000,
              "Actual Loops": 1,
              "Shared Hit Blocks": 19,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0
            }
          ]
        },
        {
          "Node Type": "CTE Scan",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "CTE Name": "s",
          "Alias": "s",
          "Startup Cost": 0.00,
          "Total Cost": 40.00,
          "Plan Rows": 2000,
          "Plan Width": 0,
          "Actual Startup Time": 0.951,
          "Actual Total Time": 1.181,
          "Actual Rows": 2000,
          "Actual Loops": 1,
          "Shared Hit Blocks": 19,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 27,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.097,
    "Triggers": [
    ],
    "Execution Time": 1.300
  }
]
"""

SUBPLAN_EXPLAIN_JSON = """
[
  {
    "Plan": {
      "Node Type": "Aggregate",
      "Strategy": "Plain",
      "Partial Mode": "Simple",
      "Parallel Aware": false,
      "Async Capable": false,
      "Startup Cost": 1423.32,
      "Total Cost": 1423.33,
      "Plan Rows": 1,
      "Plan Width": 8,
      "Actual Startup Time": 9.045,
      "Actual Total Time": 9.046,
      "Actual Rows": 1,
      "Actual Loops": 1,
      "Shared Hit Blocks": 64,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0,
      "Plans": [
        {
          "Node Type": "Seq Scan",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Async Capable": false,
          "Relation Name": "o",
          "Alias": "o",
          "Startup Cost": 0.00,
          "Total Cost": 1423.30,
          "Plan Rows": 7,
          "Plan Width": 0,
          "Actual Startup Time": 0.788,
          "Actual Total Time": 9.044,
          "Actual Rows": 7,
          "Actual Loops": 1,
          "Filter": "(k < (SubPlan 1))",
          "Rows Removed by Filter": 13,
          "Shared Hit Blocks": 64,
          "Shared Read Blocks": 0,
          "Shared Dirtied Blocks": 0,
          "Shared Written Blocks": 0,
          "Local Hit Blocks": 0,
          "Local Read Blocks": 0,
          "Local Dirtied Blocks": 0,
          "Local Written Blocks": 0,
          "Temp Read Blocks": 0,
          "Temp Written Blocks": 0,
          "Plans": [
            {
              "Node Type": "Aggregate",
              "Strategy": "Plain",
              "Partial Mode": "Simple",
              "Parent Relationship": "SubPlan",
              "Subplan Name": "SubPlan 1",
              "Parallel Aware": false,
              "Async Capable": false,
              "Startup Cost": 71.09,
              "Total Cost": 71.10,
              "Plan Rows": 1,
              "Plan Width": 8,
              "Actual Startup Time": 0.452,
              "Actual Total Time": 0.452,
              "Actual Rows": 1,
              "Actual Loops": 20,
              "Shared Hit Blocks": 63,
              "Shared Read Blocks": 0,
              "Shared Dirtied Blocks": 0,
              "Shared Written Blocks": 0,
              "Local Hit Blocks": 0,
              "Local Read Blocks": 0,
              "Local Dirtied Blocks": 0,
              "Local Written Blocks": 0,
              "Temp Read Blocks": 0,
              "Temp Written Blocks": 0,
              "Plans": [
                {
                  "Node Type": "Hash Join",
                  "Parent Relationship": "Outer",
                  "Parallel Aware": false,
                  "Async Capable": false,
                  "Join Type": "Inner",
                  "Startup Cost": 14.25,
                  "Total Cost": 62.74,
                  "Plan Rows": 3340,
                  "Plan Width": 0,
                  "Actual Startup Time": 0.012,
                  "Actual Total Time": 0.277,
                  "Actual Rows": 5800,
                  "Actual Loops": 20,
                  "Inner Unique": false,
                  "Hash Cond": "(u.v = w.x)",
                  "Shared Hit Blocks": 63,
                  "Shared Read Blocks": 0,
                  "Shared Dirtied Blocks": 0,
                  "Shared Written Blocks": 0,
                  "Local Hit Blocks": 0,
                  "Local Read Blocks": 0,
                  "Local Dirtied Blocks": 0,
                  "Local Written Blocks": 0,
                  "Temp Read Blocks": 0,
                  "Temp Written Blocks": 0,
                  "Plans": [
                    {
                      "Node Type": "Seq Scan",
                      "Parent Relationship": "Outer",
                      "Parallel Aware": false,
                      "Async Capable": false,
                      "Relation Name": "u",
                      "Alias": "u",
                      "Startup Cost": 0.00,
                      "Total Cost": 10.50,
                      "Plan Rows": 167,
                      "Plan Width": 8,
                      "Actual Startup Time": 0.007,
                      "Actual Total Time": 0.021,
                      "Actual Rows": 290,
                      "Actual Loops": 20,
                      "Filter": "(id > (o.id * 20))",
                      "Rows Removed by Filter": 210,
                      "Shared Hit Blocks": 60,
                      "Shared Read Blocks": 0,
                      "Shared Dirtied Blocks": 0,
                      "Shared Written Blocks": 0,
                      "Local Hit Blocks": 0,
                      "Local Read Blocks": 0,
                      "Local Dirtied Blocks": 0,
                      "Local Written Blocks": 0,
                      "Temp Read Blocks": 0,
                      "Temp Written Blocks": 0
                    },
                    {
                      "Node Type": "Hash",
                      "Parent Relationship": "Inner",
                      "Parallel Aware": false,
                      "Async Capable": false,
                      "Startup Cost": 8.00,
                      "Total Cost": 8.00,
                      "Plan Rows": 500,
                      "Plan Width": 8,
                      "Actual Startup Time": 0.053,
                      "Actual Total Time": 0.054,
                      "Actual Rows": 500,
                      "Actual Loops": 1,
                      "Hash Buckets": 1024,
                      "Original Hash Buckets": 1024,
                      "Hash Batches": 1,
                      "Original Hash Batches": 1,
                      "Peak Memory Usage": 28,
                      "Shared Hit Blocks": 3,
                      "Shared Read Blocks": 0,
                      "Shared Dirtied Blocks": 0,
                      "Shared Written Blocks": 0,
                      "Local Hit Blocks": 0,
                      "Local Read Blocks": 0,
                      "Local Dirtied Blocks": 0,
                      "Local Written Blocks": 0,
                      "Temp Read Blocks": 0,
                      "Temp Written Blocks": 0,
                      "Plans": [
                        {
                          "Node Type": "Seq Scan",
                          "Parent Relationship": "Outer",
                          "Parallel Aware": false,
                          "Async Capable": false,
                          "Relation Name": "w",
                          "Alias": "w",
                          "Startup Cost": 0.00,
                          "Total Cost": 8.00,
                          "Plan Rows": 500,
                          "Plan Width": 8,
                          "Actual Startup Time": 0.002,
                          "Actual Total Time": 0.022,
                          "Actual Rows": 500,
                          "Actual Loops": 1,
                          "Shared Hit Blocks": 3,
                          "Shared Read Blocks": 0,
                          "Shared Dirtied Blocks": 0,
                          "Shared Written Blocks": 0,
                          "Local Hit Blocks": 0,
                          "Local Read Blocks": 0,
                          "Local Dirtied Blocks": 0,
                          "Local Written Blocks": 0,
                          "Temp Read Blocks": 0,
                          "Temp Written Blocks": 0
                        }
                      ]
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    },
    "Planning": {
      "Shared Hit Blocks": 92,
      "Shared Read Blocks": 0,
      "Shared Dirtied Blocks": 0,
      "Shared Written Blocks": 0,
      "Local Hit Blocks": 0,
      "Local Read Blocks": 0,
      "Local Dirtied Blocks": 0,
      "Local Written Blocks": 0,
      "Temp Read Blocks": 0,
      "Temp Written Blocks": 0
    },
    "Planning Time": 0.155,
    "Triggers": [
    ],
    "Execution Time": 9.072
  }
]
"""


def _tree_total(node, key):
    """`key` summed over every node -- the figure `_work` must NOT report."""
    return int(node.get(key, 0)) + sum(_tree_total(c, key) for c in node.get("Plans", []))


def test_a_cte_plans_work_is_the_root_total_not_a_sum_over_its_initplan():
    """The materialized CTE runs as an InitPlan child ("CTE s") of the
    root, and its pages are already inside the root's 19 hits. Summing
    the tree would report 76."""
    explain = json.loads(CTE_EXPLAIN_JSON)
    root = explain[0]["Plan"]
    work, _m, _t, _h, _ms = parse_plan(explain)
    assert work == root["Shared Hit Blocks"] + root["Shared Read Blocks"] == 19
    assert _tree_total(root, "Shared Hit Blocks") == 76


def test_peak_memory_reaches_a_sort_under_a_cte_initplan():
    explain = json.loads(CTE_EXPLAIN_JSON)
    initplan = explain[0]["Plan"]["Plans"][0]
    assert (initplan["Parent Relationship"], initplan["Subplan Name"]) == ("InitPlan", "CTE s")
    assert initplan["Node Type"] == "Sort"
    assert initplan["Sort Space Type"] == "Memory"
    _w, mem, _t, _h, _ms = parse_plan(explain)
    assert mem == initplan["Sort Space Used"] == 205


def test_a_subplans_work_is_the_root_total_though_it_ran_twenty_times():
    """The correlated SubPlan executed 20 times ("Actual Loops": 20). Its
    63 hits are cumulative across those loops and already inside the
    root's 64: neither summing the tree (320) nor multiplying by loops is
    the work done."""
    explain = json.loads(SUBPLAN_EXPLAIN_JSON)
    root = explain[0]["Plan"]
    subplan = root["Plans"][0]["Plans"][0]
    assert (subplan["Parent Relationship"], subplan["Actual Loops"]) == ("SubPlan", 20)
    work, _m, _t, _h, _ms = parse_plan(explain)
    assert work == root["Shared Hit Blocks"] + root["Shared Read Blocks"] == 64
    assert _tree_total(root, "Shared Hit Blocks") == 320


def test_peak_memory_reaches_a_hash_under_a_subplan():
    explain = json.loads(SUBPLAN_EXPLAIN_JSON)
    subplan = explain[0]["Plan"]["Plans"][0]["Plans"][0]
    assert (subplan["Parent Relationship"], subplan["Subplan Name"]) == ("SubPlan", "SubPlan 1")
    hash_node = subplan["Plans"][0]["Plans"][1]
    assert hash_node["Node Type"] == "Hash"
    _w, mem, _t, _h, _ms = parse_plan(explain)
    assert mem == hash_node["Peak Memory Usage"] == 28
