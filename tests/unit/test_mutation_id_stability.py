from __future__ import annotations

from sqlproof.mutation.apply import prepare_mutants
from sqlproof.mutation.model import Mutant, MutationSet, Replace


def _mutation_set() -> MutationSet:
    return MutationSet(
        mutants=(
            Mutant(target_kind="function", target_name="f", ops=(Replace("1", "2"),)),
        )
    )


def test_mutant_id_is_stable_across_schema_formatting() -> None:
    # Same logical function, different surrounding whitespace/formatting.
    compact = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"
    spaced = """
        CREATE   FUNCTION f()
          RETURNS int
          LANGUAGE sql
          AS $$   SELECT    1   $$;
    """
    id_compact = prepare_mutants(_mutation_set(), compact)[0].mutant_id
    id_spaced = prepare_mutants(_mutation_set(), spaced)[0].mutant_id
    assert id_compact == id_spaced


def test_distinct_mutations_get_distinct_ids() -> None:
    schema = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 + 1 $$;"
    set_a = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1 + 1", "2"),)),)
    )
    set_b = MutationSet(
        mutants=(Mutant(target_kind="function", target_name="f", ops=(Replace("1 + 1", "3"),)),)
    )
    id_a = prepare_mutants(set_a, schema)[0].mutant_id
    id_b = prepare_mutants(set_b, schema)[0].mutant_id
    assert id_a != id_b
