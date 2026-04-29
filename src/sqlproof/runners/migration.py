from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from hypothesis import HealthCheck, given, settings

from sqlproof.generators.graph import dataset_strategy


def migration(
    proof: Any,
    *,
    before_schema: str,
    migration: str,
    sizes: dict[str, int],
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del before_schema
    runs = int(cast(Any, kwargs.pop("runs", 1)))

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            @given(dataset_strategy(proof.schema_info, sizes=sizes))
            @settings(
                max_examples=runs,
                deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture],
            )
            def execute(dataset: dict[str, list[dict[str, Any]]]) -> None:
                with (
                    proof.client_for_dataset(dataset) as before,
                    proof.client_for_dataset(dataset) as after,
                ):
                    _execute_migration(after, migration)
                    function(before, after)

            execute()

        return wrapped

    return decorate


def _execute_migration(db: Any, migration: str) -> None:
    path = Path(migration)
    if path.exists():
        db.execute_file(path)
    else:
        db.execute(migration)
