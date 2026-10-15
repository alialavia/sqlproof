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


def test_without_a_seed_the_drawn_one_is_recorded(tmp_path: Path) -> None:
    seed = _fail(tmp_path, None)["seed"]
    assert isinstance(seed, int)


def test_a_derandomized_profile_stays_derandomized(tmp_path: Path) -> None:
    settings.register_profile("sqlproof-test-derandomize", derandomize=True, database=None)
    previous = settings.get_current_profile_name()
    settings.load_profile("sqlproof-test-derandomize")
    try:
        first = _fail(tmp_path / "a", None)
        second = _fail(tmp_path / "b", None)
    finally:
        settings.load_profile(previous)
    assert first["seed"] is None and second["seed"] is None
    assert first["dataset"] == second["dataset"]
