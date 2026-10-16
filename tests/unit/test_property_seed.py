"""Which seed a property run uses and records (scenarios phase 4, review).

Only an explicit `seed=` is forced on Hypothesis: forcing one also turns
off its example database and overrides `derandomize` and
`--hypothesis-seed`. Otherwise the counterexample records the seed
Hypothesis drew, or none when the run was derandomized.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st

from sqlproof import SqlProof
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.runners.property import run_property


class FakeProof:
    schema_fingerprint = "f"

    def dataset_strategy(self, sizes: Any, columns: Any) -> Any:
        return st.fixed_dictionaries({"t": st.lists(st.integers(0, 10**6), min_size=1, max_size=3)})

    @contextlib.contextmanager
    def client_for_dataset(self, dataset: Any) -> Iterator[Any]:
        yield dataset


def _fail(tmp_path: Path, seed: int | None) -> dict[str, Any]:
    def always_fails(db: Any) -> None:
        raise AssertionError("no")

    with pytest.raises(SqlProofPropertyFailure) as failure:
        run_property(FakeProof(), always_fails, sizes={}, runs=5, failure_dir=tmp_path, seed=seed)
    payload = failure.value.counterexample
    assert payload is not None
    return payload


def test_an_explicit_seed_is_recorded(tmp_path: Path) -> None:
    assert _fail(tmp_path, 1234)["seed"] == 1234


@contextlib.contextmanager
def _profile(name: str, **options: Any) -> Iterator[None]:
    """Run under a Hypothesis profile (CI runners load the derandomized
    `ci` profile by default), restoring the previous one after."""
    settings.register_profile(name, database=None, **options)
    previous = settings.get_current_profile_name()
    settings.load_profile(name)
    try:
        yield
    finally:
        settings.load_profile(previous)


def test_without_a_seed_the_drawn_one_is_recorded(tmp_path: Path) -> None:
    with _profile("sqlproof-test-random", derandomize=False):
        seed = _fail(tmp_path, None)["seed"]
    assert isinstance(seed, int)


def test_a_derandomized_profile_stays_derandomized(tmp_path: Path) -> None:
    with _profile("sqlproof-test-derandomize", derandomize=True):
        first = _fail(tmp_path / "a", None)
        second = _fail(tmp_path / "b", None)
    assert first["seed"] is None and second["seed"] is None
    assert first["dataset"] == second["dataset"]


@pytest.fixture
def recorded_proof(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE numbers (value integer NOT NULL)", encoding="utf-8")
    proof = SqlProof.from_schema_file(schema)
    observed = []
    original = proof.client_for_dataset

    @contextlib.contextmanager
    def record(dataset):
        observed.append(dataset)
        with original(dataset) as client:
            yield client

    monkeypatch.setattr(proof, "client_for_dataset", record)
    return proof, observed


def _imperative_run(proof, method, seed, *, expect_failure=False):
    if method == "check":

        def property_holds(db):
            assert not expect_failure, "deliberate failure"

        proof.check(
            "seeded numbers", sizes={"numbers": 1}, property=property_holds, runs=10, seed=seed
        )
    else:
        proof.invariant(
            "seeded numbers",
            sizes={"numbers": 1},
            query="SELECT value FROM numbers",
            expect_empty=expect_failure,
            runs=10,
            seed=seed,
        )


@pytest.mark.parametrize("method", ["check", "invariant"])
@pytest.mark.parametrize("seed", [0, 1234])
def test_the_same_seed_repeats_the_generated_datasets(recorded_proof, method, seed) -> None:
    """Oracle: two independent calls with one seed must have the same trace."""
    proof, observed = recorded_proof
    with _profile("sqlproof-test-imperative", derandomize=False):
        _imperative_run(proof, method, seed)
        first = list(observed)
        observed.clear()
        _imperative_run(proof, method, seed)
    assert len(first) == 10
    assert observed == first


@pytest.mark.parametrize("method", ["check", "invariant"])
def test_different_seeds_explore_different_datasets(recorded_proof, method) -> None:
    """A constant dataset stream must not satisfy the repeatability check."""
    proof, observed = recorded_proof
    _imperative_run(proof, method, 0)
    first = list(observed)
    observed.clear()
    _imperative_run(proof, method, 1234)
    assert observed != first


@pytest.mark.parametrize("method", ["check", "invariant"])
def test_imperative_failures_record_the_requested_seed(recorded_proof, method) -> None:
    """Oracle: the public exception must carry the caller's replay seed."""
    proof, _ = recorded_proof
    with pytest.raises(SqlProofPropertyFailure) as failure:
        _imperative_run(proof, method, 0, expect_failure=True)
    assert failure.value.counterexample["seed"] == 0


def test_unseeded_invariants_record_the_seed_hypothesis_chose(recorded_proof) -> None:
    """Oracle: the failure carries a replay seed even when none was supplied."""
    proof, _ = recorded_proof
    with (
        _profile("sqlproof-test-invariant-random", derandomize=False),
        pytest.raises(SqlProofPropertyFailure) as failure,
    ):
        _imperative_run(proof, "invariant", None, expect_failure=True)
    assert isinstance(failure.value.counterexample["seed"], int)


def test_a_derandomized_invariant_does_not_invent_a_seed(recorded_proof) -> None:
    """A derandomized profile is reproducible without a numeric seed."""
    proof, _ = recorded_proof
    with (
        _profile("sqlproof-test-invariant-derandomize", derandomize=True),
        pytest.raises(SqlProofPropertyFailure) as failure,
    ):
        _imperative_run(proof, "invariant", None, expect_failure=True)
    assert failure.value.counterexample["seed"] is None


def test_an_invariant_shrinks_to_the_smallest_violating_dataset(recorded_proof) -> None:
    """Oracle: exactly one row is the smallest nonempty result set."""
    proof, _ = recorded_proof
    with pytest.raises(SqlProofPropertyFailure) as failure:
        proof.invariant(
            "no numbers",
            sizes={"numbers": st.integers(0, 6)},
            query="SELECT value FROM numbers",
            runs=20,
            seed=1234,
        )
    counterexample = failure.value.counterexample
    assert counterexample["property_name"] == "no numbers"
    assert counterexample["dataset"] == {"numbers": [{"value": 0}]}
