from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from hypothesis import HealthCheck, given, settings

from sqlproof.generators.graph import dataset_strategy


def stateful(
    proof: Any, *, sizes: dict[str, int], **kwargs: object
) -> Callable[[type[Any]], type[Any]]:
    runs = int(cast(Any, kwargs.pop("runs", 1)))

    def decorate(cls: type[Any]) -> type[Any]:
        original_run = getattr(cls, "run", None)

        def run(self: Any) -> None:
            @given(dataset_strategy(proof.schema_info, sizes=sizes))
            @settings(
                max_examples=runs,
                deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture],
            )
            def execute(dataset: dict[str, list[dict[str, Any]]]) -> None:
                with proof.client_for_dataset(dataset) as db:
                    if original_run is not None:
                        original_run(self, db)

            execute()

        cls.run = run
        return cls

    return decorate
