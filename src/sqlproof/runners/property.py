from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from hypothesis import HealthCheck, given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.generators.graph import ColumnOverrides, SizeSpec, dataset_strategy
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
    sizes: Mapping[str, SizeSpec],
    columns: ColumnOverrides | None = None,
    runs: int = 100,
    seed: int | None = None,
    timeout_ms: int = 5000,
    commit: bool = False,
    failure_dir: str | Path = ".sqlproof/failures",
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Run `function` as a property test against generated datasets.

    `columns` forwards to ``dataset_strategy``'s per-column override
    map — pin specific values, narrow ranges, or substitute a strategy.

    The decorated property function may declare any of these
    parameters, in any order:

      * ``db`` (always present) — the ``SqlProofClient`` with the
        generated dataset loaded.
      * ``dataset`` — the generated dataset dict (table name → rows).
        Use when assertions need to know what was generated, e.g. to
        compare a SQL aggregate against a Python recomputation.
      * ``check`` — the per-run ``Check`` helper for adding row context
        to counterexamples.

    Parameters not declared in the signature are not passed.

    `seed` fixes Hypothesis's random choices (and, as Hypothesis's `@seed`
    does, turns off its example database for the run). Without one,
    Hypothesis chooses as usual -- `derandomize` and `--hypothesis-seed`
    apply -- and the counterexample records the seed it drew. Passing that
    seed back repeats the run's random search. It does not reproduce a
    failure Hypothesis replayed from its example database (one an earlier
    run found): that run's seed never searched for it, and with the
    database off the search may not find it again. Keep the `.hypothesis/`
    directory, or the counterexample file, to reproduce those.
    """
    del timeout_ms, commit

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            run_property(
                proof,
                function,
                sizes=sizes,
                columns=columns,
                runs=runs,
                failure_dir=Path(failure_dir),
                seed=seed,
            )

        wrapped.__name__ = function.__name__
        wrapped.__doc__ = function.__doc__
        return wrapped

    return decorate


def run_property(
    proof: Any,
    function: Callable[..., None],
    *,
    sizes: Mapping[str, SizeSpec],
    columns: ColumnOverrides | None = None,
    runs: int,
    failure_dir: Path,
    seed: int | None = None,
) -> None:
    if hasattr(proof, "dataset_strategy"):
        strategy = proof.dataset_strategy(sizes=sizes, columns=columns)
    else:
        strategy = dataset_strategy(proof.schema_info, sizes=sizes, columns=columns)
    signature = inspect.signature(function)
    wants_check = "check" in signature.parameters
    wants_dataset = "dataset" in signature.parameters
    counter = _RunCounter()

    def execute(dataset: dict[str, list[dict[str, Any]]]) -> None:
        counter.start()
        check = Check()
        try:
            with proof.client_for_dataset(dataset) as client:
                kwargs: dict[str, Any] = {}
                if wants_dataset:
                    kwargs["dataset"] = dataset
                if wants_check:
                    kwargs["check"] = check
                function(client, **kwargs)
        except Exception as exc:
            runs_used, shrink_steps = counter.failed()
            payload: dict[str, Any] = {
                "$schema": "https://sqlproof.dev/schemas/counterexample-v1.json",
                "version": 1,
                "property_name": function.__name__,
                "seed": _seed_in_use(wrapped, seed),
                "runs": runs_used,
                "shrink_steps": shrink_steps,
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

    key = f"sqlproof.property:{function.__module__}.{function.__qualname__}"
    wrapped = _search(execute, strategy, runs=runs, seed=seed, key=key)
    wrapped()


def run_invariant(
    proof: Any,
    *,
    name: str,
    sizes: Mapping[str, SizeSpec],
    query: str,
    expect_empty: bool,
    runs: int,
    seed: int | None = None,
    failure_dir: Path = Path(".sqlproof/failures"),
) -> None:
    """Search for a violating dataset without wrapping database errors."""
    strategy = proof.dataset_strategy(sizes=sizes)
    counter = _RunCounter()

    def execute(dataset: dict[str, list[dict[str, Any]]]) -> None:
        counter.start()
        with proof.client_for_dataset(dataset) as client:
            rows = client.query(query)
        failed = bool(rows) if expect_empty else not rows
        if failed:
            runs_used, shrink_steps = counter.failed()
            message = f"Invariant {name!r} failed: query returned {len(rows)} rows."
            payload: dict[str, Any] = {
                "$schema": "https://sqlproof.dev/schemas/counterexample-v1.json",
                "version": 1,
                "property_name": name,
                "seed": _seed_in_use(wrapped, seed),
                "runs": runs_used,
                "shrink_steps": shrink_steps,
                "row_context": {},
                "dataset": dataset,
                "schema_fingerprint": proof.schema_fingerprint,
                "failure": {
                    "kind": "InvariantViolation",
                    "message": message,
                    "locals": {},
                    "traceback": [],
                },
            }
            write_counterexample(failure_dir / f"{_file_stem(name)}.json", payload)
            raise SqlProofPropertyFailure(message, counterexample=payload)

    key = f"sqlproof.invariant:{name}\0{query}"
    wrapped = _search(execute, strategy, runs=runs, seed=seed, key=key)
    wrapped()


class _RunCounter:
    """Counts runs so a counterexample reports the run that first failed.

    Hypothesis calls the test again while shrinking. Those calls are
    reported as `shrink_steps`, not as runs, so a property that fails on
    its first dataset reports `runs: 1` however long shrinking takes.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.first_failure: int | None = None

    def start(self) -> None:
        self.calls += 1

    def failed(self) -> tuple[int, int]:
        if self.first_failure is None:
            self.first_failure = self.calls
        return self.first_failure, self.calls - self.first_failure


def _search(
    execute: Callable[[dict[str, list[dict[str, Any]]]], None],
    strategy: SearchStrategy[Any],
    *,
    runs: int,
    seed: int | None,
    key: str,
) -> Callable[[], None]:
    """Wrap `execute` as a Hypothesis search over `strategy`.

    Every property shares the same `execute` source, and Hypothesis keys
    its example database by source. Adding the property's own key keeps
    one property's saved failures from being replayed against, or deleted
    by, another. `report_multiple_bugs=False` keeps the public error types:
    two distinct failures would otherwise surface as an ExceptionGroup.
    """
    cast(Any, execute)._hypothesis_internal_add_digest = key.encode()
    wrapped = given(strategy)(
        settings(
            max_examples=runs,
            deadline=None,
            report_multiple_bugs=False,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        )(execute)
    )
    if seed is not None:
        # Only an explicit seed is forced: `@seed` also turns off the example
        # database and overrides `derandomize` and `--hypothesis-seed`.
        wrapped = hypothesis_seed(seed)(wrapped)
    return wrapped


def _file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return stem or "invariant"


def _seed_in_use(wrapped: Any, explicit: int | None) -> int | None:
    """The seed this run used, for the counterexample: the one passed in,
    else the one Hypothesis drew (or was forced to with `--hypothesis-seed`).
    None when the run was derandomized, which reproduces without a seed."""
    if explicit is not None:
        return explicit
    from hypothesis import core

    # Hypothesis records the seed it drew on the wrapped test.
    generated = getattr(wrapped, "_hypothesis_internal_use_generated_seed", None)
    if generated is not None:
        return int(generated)
    forced = getattr(core, "global_force_seed", None)
    return None if forced is None else int(forced)
