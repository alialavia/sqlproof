from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlproof.client import InMemorySqlProofClient
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example


def rls(
    proof: Any,
    *,
    sizes: dict[str, int],
    roles: list[str],
    mode: str = "postgrest",
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del mode, kwargs

    def decorate(function: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            dataset = draw_example(dataset_strategy(proof.schema_info, sizes=sizes))
            for role in roles:
                function(InMemorySqlProofClient(dataset), {"role": role}, dataset)

        return wrapped

    return decorate
