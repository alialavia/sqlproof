from __future__ import annotations

import inspect
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from hypothesis import HealthCheck, given, settings

from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.generators.graph import dataset_strategy
from sqlproof.reporter.json_io import write_counterexample

P = ParamSpec("P")
R = TypeVar("R")


class Check:
    def __init__(self) -> None:
        self.row_context: dict[str, Any] | None = None

    @contextmanager
    def row(self, **context: Any) -> Generator[None]:
        previous = self.row_context
        self.row_context = context
        try:
            yield
        finally:
            if previous is not None:
                self.row_context = previous

    def label(self, name: str) -> None:
        del name


def sqlproof(
    proof: Any,
    *,
    sizes: dict[str, int],
    runs: int = 100,
    seed: int | None = None,
    timeout_ms: int = 5000,
    commit: bool = False,
    failure_dir: str | Path = ".sqlproof/failures",
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del seed, timeout_ms, commit

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            run_property(
                proof,
                function,
                sizes=sizes,
                runs=runs,
                failure_dir=Path(failure_dir),
            )

        wrapped.__name__ = function.__name__
        wrapped.__doc__ = function.__doc__
        return wrapped

    return decorate


def run_property(
    proof: Any,
    function: Callable[..., None],
    *,
    sizes: dict[str, int],
    runs: int,
    failure_dir: Path,
) -> None:
    strategy = dataset_strategy(proof.schema_info, sizes=sizes)
    signature = inspect.signature(function)
    wants_check = "check" in signature.parameters
    run_count = 0

    @given(strategy)
    @settings(
        max_examples=runs,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def execute(dataset: dict[str, list[dict[str, Any]]]) -> None:
        nonlocal run_count
        run_count += 1
        check = Check()
        try:
            with proof.client_for_dataset(dataset) as client:
                if wants_check:
                    function(client, check)
                else:
                    function(client)
        except Exception as exc:
            payload: dict[str, Any] = {
                "$schema": "https://sqlproof.dev/schemas/counterexample-v1.json",
                "version": 1,
                "property_name": function.__name__,
                "seed": None,
                "runs": run_count,
                "shrink_steps": 0,
                "schema_fingerprint": proof.schema_fingerprint,
                "row_context": check.row_context or {},
                "dataset": dataset,
                "failure": {
                    "kind": type(exc).__name__,
                    "message": str(exc),
                    "locals": {},
                    "traceback": [],
                },
            }
            write_counterexample(failure_dir / f"{function.__name__}.json", payload)
            raise SqlProofPropertyFailure(str(exc), counterexample=payload) from exc

    execute()
