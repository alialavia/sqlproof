from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from hypothesis import HealthCheck, given, settings

from sqlproof.generators.graph import dataset_strategy


def rls(
    proof: Any,
    *,
    sizes: dict[str, int],
    roles: list[str],
    mode: str = "postgrest",
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del mode
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
                for role in roles:
                    with proof.client_for_dataset(dataset) as db:
                        db.execute(f"SET LOCAL ROLE {role}")
                        function(db, {"role": role}, dataset)

            execute()

        return wrapped

    return decorate
