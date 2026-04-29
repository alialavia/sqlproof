from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlproof.client import InMemorySqlProofClient
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example


def migration(
    proof: Any,
    *,
    before_schema: str,
    migration: str,
    sizes: dict[str, int],
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del before_schema, migration, kwargs

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            dataset = draw_example(dataset_strategy(proof.schema_info, sizes=sizes))
            function(InMemorySqlProofClient(dataset), InMemorySqlProofClient(dataset))

        return wrapped

    return decorate
