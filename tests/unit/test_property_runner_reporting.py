"""What a failing property or invariant reports, and what it keeps.

The fake proofs below log every call Hypothesis makes, so the log is an
independent record of which run failed first and how many calls
shrinking took.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.database import InMemoryExampleDatabase

from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.runners.property import run_invariant, run_property


class LoggingProof:
    """Datasets of one small list of integers; the client is the dataset."""

    schema_fingerprint = "f"

    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def dataset_strategy(self, sizes: Any, columns: Any = None) -> Any:
        del sizes, columns
        return st.fixed_dictionaries({"t": st.lists(st.integers(0, 10**6), min_size=1, max_size=3)})

    @contextlib.contextmanager
    def client_for_dataset(self, dataset: Any) -> Iterator[Any]:
        self.calls.append(list(dataset["t"]))
        yield _Client(dataset)


class _Client:
    def __init__(self, dataset: Any) -> None:
        self.values = dataset["t"]

    def query(self, sql: str) -> list[int]:
        # "large" rows are the invariant's violations.
        del sql
        return [value for value in self.values if value >= 900_000]


@contextlib.contextmanager
def _profile(**options: Any) -> Iterator[None]:
    name = f"reporting-{len(options)}-{id(options)}"
    settings.register_profile(name, derandomize=False, **options)
    previous = settings.get_current_profile_name()
    settings.load_profile(name)
    try:
        yield
    finally:
        settings.load_profile(previous)


def _first_failing_call(calls: list[list[int]]) -> int:
    return next(index for index, values in enumerate(calls, 1) if max(values) >= 900_000)


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_a_property_that_fails_in_two_ways_raises_one_property_failure(
    tmp_path: Path, seed: int
) -> None:
    """Contract: check() and @sqlproof raise SqlProofPropertyFailure, never an
    ExceptionGroup, even when different inputs fail with different errors."""

    def two_failure_modes(db: Any) -> None:
        if min(db.values) < 500_000:
            raise ZeroDivisionError("small")
        raise AssertionError("large")

    with pytest.raises(SqlProofPropertyFailure) as failure:
        run_property(
            LoggingProof(), two_failure_modes, sizes={}, runs=100,
            failure_dir=tmp_path, seed=seed,
        )
    assert type(failure.value) is SqlProofPropertyFailure


@pytest.mark.parametrize("runner", ["property", "invariant"])
@pytest.mark.parametrize("seed", [0, 7, 99])
def test_runs_count_the_run_that_first_failed_and_shrinking_counts_separately(
    tmp_path: Path, runner: str, seed: int
) -> None:
    """Oracle: the proof's own call log. `runs` is the 1-based call that
    first failed; every later call is a shrink step."""
    proof = LoggingProof()

    def fails_on_large_values(db: Any) -> None:
        assert max(db.values) < 900_000

    with pytest.raises(SqlProofPropertyFailure) as failure:
        if runner == "property":
            run_property(
                proof, fails_on_large_values, sizes={}, runs=500,
                failure_dir=tmp_path, seed=seed,
            )
        else:
            run_invariant(
                proof, name="no large values", sizes={}, query="SELECT",
                expect_empty=True, runs=500, seed=seed, failure_dir=tmp_path,
            )
    payload = failure.value.counterexample
    assert payload is not None
    assert payload["runs"] == _first_failing_call(proof.calls)
    assert payload["runs"] + payload["shrink_steps"] == len(proof.calls)
    assert payload["runs"] <= 500


def test_an_invariant_saves_its_counterexample_under_a_safe_file_name(tmp_path: Path) -> None:
    """Contract: like check(), a failing invariant leaves a counterexample file,
    named from the invariant's free-text name without path separators."""
    with pytest.raises(SqlProofPropertyFailure) as failure:
        run_invariant(
            LoggingProof(), name="no rows / ever: really", sizes={}, query="SELECT",
            expect_empty=False, runs=5, seed=0, failure_dir=tmp_path,
        )
    saved = list(tmp_path.iterdir())
    assert [path.name for path in saved] == ["no_rows_ever_really.json"]
    stored = json.loads(saved[0].read_text())
    assert stored["property_name"] == "no rows / ever: really"
    assert stored["runs"] == failure.value.counterexample["runs"]


def test_a_passing_property_keeps_another_propertys_saved_failure(tmp_path: Path) -> None:
    """Contract: each property has its own example-database entry. A shared
    entry would replay A's failure against B, and B passing would delete it."""
    database = InMemoryExampleDatabase()

    def fails_on_large_values(db: Any) -> None:
        assert max(db.values) < 900_000

    def always_passes(db: Any) -> None:
        del db

    with _profile(database=database):
        with pytest.raises(SqlProofPropertyFailure):
            run_property(
                LoggingProof(), fails_on_large_values, sizes={}, runs=500, failure_dir=tmp_path
            )
        saved = {key: set(values) for key, values in database.data.items() if values}
        assert saved, "the failing property saved its counterexample"
        run_property(LoggingProof(), always_passes, sizes={}, runs=50, failure_dir=tmp_path)
        for key, values in saved.items():
            assert values <= set(database.data.get(key, ())), "B removed A's saved failure"
        replay = LoggingProof()
        with pytest.raises(SqlProofPropertyFailure) as again:
            run_property(replay, fails_on_large_values, sizes={}, runs=500, failure_dir=tmp_path)
    assert again.value.counterexample is not None
    assert again.value.counterexample["runs"] == 1, "the saved failure replays first"


def test_named_properties_sharing_a_function_keep_their_own_saved_failures(
    tmp_path: Path,
) -> None:
    """Contract: as above, for check()'s `name`. One factory function yields
    properties with the same qualified name; only `name` tells them apart."""
    database = InMemoryExampleDatabase()

    def below(limit: int) -> Any:
        def prop(db: Any) -> None:
            assert max(db.values) < limit

        return prop

    with _profile(database=database):
        with pytest.raises(SqlProofPropertyFailure):
            run_property(
                LoggingProof(), below(900_000), sizes={}, runs=500,
                failure_dir=tmp_path, name="strict",
            )
        saved = {key: set(values) for key, values in database.data.items() if values}
        assert saved, "the failing property saved its counterexample"
        run_property(
            LoggingProof(), below(10**7), sizes={}, runs=50, failure_dir=tmp_path, name="loose"
        )
        for key, values in saved.items():
            assert values <= set(database.data.get(key, ())), "loose removed strict's failure"


class LargeDatasetProof(LoggingProof):
    """Datasets far bigger than Hypothesis's default size health checks allow."""

    def dataset_strategy(self, sizes: Any, columns: Any = None) -> Any:
        del sizes, columns
        return st.fixed_dictionaries(
            {"t": st.lists(st.integers(0, 10), min_size=3000, max_size=3000)}
        )


@pytest.mark.parametrize("runner", ["property", "invariant"])
def test_large_datasets_run_instead_of_failing_a_health_check(tmp_path: Path, runner: str) -> None:
    """Regression: invariant() drew with `.example()` before it became a
    search, so big `sizes` worked; the search must not reject them either."""
    proof = LargeDatasetProof()
    if runner == "property":
        run_property(proof, lambda db: None, sizes={}, runs=2, failure_dir=tmp_path, seed=1)
    else:
        run_invariant(
            proof, name="large", sizes={}, query="q", expect_empty=True, runs=2,
            seed=1, failure_dir=tmp_path,
        )
    assert proof.calls and all(len(values) == 3000 for values in proof.calls)
