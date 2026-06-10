from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.model import Drop, MutationSet, Replace


def test_for_function_creates_one_mutant_per_op() -> None:
    mutations = MutationSet.for_function(
        "get_user_usage_total",
        [
            Replace("feature = p_feature", "feature <> p_feature"),
            Drop("WHERE user_id = p_user_id"),
        ],
    )
    assert len(mutations.mutants) == 2
    first, second = mutations.mutants
    assert first.target_kind == "function"
    assert first.target_name == "get_user_usage_total"
    assert first.ops == (Replace("feature = p_feature", "feature <> p_feature"),)
    assert second.ops == (Drop("WHERE user_id = p_user_id"),)


def test_for_function_rejects_empty_ops() -> None:
    with pytest.raises(SqlProofMutationError, match="at least one"):
        MutationSet.for_function("f", [])


def test_replace_rejects_identical_old_and_new() -> None:
    with pytest.raises(SqlProofMutationError, match="no-op"):
        Replace("x", "x")


def test_expect_survives_requires_reason() -> None:
    with pytest.raises(SqlProofMutationError, match="reason"):
        Replace("a", "b", expect_survives=True)
    with pytest.raises(SqlProofMutationError, match="reason"):
        Drop("a", expect_survives=True)


def test_reason_requires_expect_survives() -> None:
    with pytest.raises(SqlProofMutationError, match="expect_survives"):
        Replace("a", "b", reason="dead code")


def test_expect_survives_is_lifted_onto_the_mutant() -> None:
    mutations = MutationSet.for_function(
        "f", [Drop("AND deleted_at IS NULL", expect_survives=True, reason="dead branch")]
    )
    (mutant,) = mutations.mutants
    assert mutant.expect_survives is True
    assert mutant.reason == "dead branch"


def test_mutation_sets_concatenate() -> None:
    a = MutationSet.for_function("f", [Drop("x")])
    b = MutationSet.for_function("g", [Drop("y")])
    combined = a + b
    assert [m.target_name for m in combined.mutants] == ["f", "g"]


def test_json_round_trip() -> None:
    mutations = MutationSet.for_function(
        "get_user_usage_total",
        [
            Replace("used_at >= p_start", "used_at > p_start"),
            Drop("AND deleted_at IS NULL", expect_survives=True, reason="dead branch"),
        ],
    )
    data = mutations.to_dict()
    restored = MutationSet.from_dict(data)
    assert restored == mutations
    # The wire format is plain JSON-able primitives.
    import json

    assert json.loads(json.dumps(data)) == data


def test_describe_is_human_readable() -> None:
    mutant = MutationSet.for_function("f", [Replace("a", "b")]).mutants[0]
    assert "f" in mutant.describe()
    assert "'a'" in mutant.describe()
    assert "'b'" in mutant.describe()


# ---------------------------------------------------------------------------
# Fix 1: Mutant.__post_init__ enforces invariants
# ---------------------------------------------------------------------------


def test_mutant_direct_empty_ops_raises() -> None:
    with pytest.raises(SqlProofMutationError, match="at least one op"):
        from sqlproof.mutation.model import Mutant

        Mutant(target_kind="function", target_name="f", ops=())


def test_mutant_direct_flagged_op_raises() -> None:
    from sqlproof.mutation.model import Mutant

    with pytest.raises(SqlProofMutationError, match="expect_survives"):
        Mutant(
            target_kind="function",
            target_name="f",
            ops=(Replace("a", "b", expect_survives=True, reason="r"),),
        )


def test_mutant_expect_survives_without_reason_raises() -> None:
    from sqlproof.mutation.model import Mutant

    with pytest.raises(SqlProofMutationError, match="reason"):
        Mutant(
            target_kind="function",
            target_name="f",
            ops=(Replace("a", "b"),),
            expect_survives=True,
        )


def test_mutant_from_dict_empty_ops_raises() -> None:
    from sqlproof.mutation.model import Mutant

    payload = {
        "target": {"kind": "function", "name": "f"},
        "ops": [],
        "expect_survives": False,
        "reason": None,
    }
    with pytest.raises(SqlProofMutationError, match="at least one op"):
        Mutant.from_dict(payload)


# ---------------------------------------------------------------------------
# Fix 2: Replace/Drop reject empty pattern strings
# ---------------------------------------------------------------------------


def test_replace_empty_old_raises() -> None:
    with pytest.raises(SqlProofMutationError, match=r"Replace\.old must be non-empty"):
        Replace("", "x")


def test_drop_empty_pattern_raises() -> None:
    with pytest.raises(SqlProofMutationError, match=r"Drop\.pattern must be non-empty"):
        Drop("")


# ---------------------------------------------------------------------------
# Fix 3: MutationSet.__add__ returns NotImplemented for non-MutationSet
# ---------------------------------------------------------------------------


def test_mutation_set_add_non_mutation_set_raises_type_error() -> None:
    ms = MutationSet.for_function("f", [Drop("x")])
    with pytest.raises(TypeError):
        ms + [1]  # type: ignore[operator]  # noqa: RUF005


# ---------------------------------------------------------------------------
# Fix 4: _op_from_dict unknown kind and unknown target kind via from_dict
# ---------------------------------------------------------------------------


def test_from_dict_unknown_op_kind_raises() -> None:
    from sqlproof.mutation.model import Mutant

    payload = {
        "target": {"kind": "function", "name": "f"},
        "ops": [{"op": "weird"}],
        "expect_survives": False,
        "reason": None,
    }
    with pytest.raises(SqlProofMutationError, match="Unknown mutation op kind"):
        Mutant.from_dict(payload)


def test_from_dict_unknown_target_kind_raises() -> None:
    from sqlproof.mutation.model import Mutant

    payload = {
        "target": {"kind": "table", "name": "users"},
        "ops": [{"op": "drop", "pattern": "x"}],
        "expect_survives": False,
        "reason": None,
    }
    with pytest.raises(SqlProofMutationError, match="Unknown mutant target kind"):
        Mutant.from_dict(payload)
