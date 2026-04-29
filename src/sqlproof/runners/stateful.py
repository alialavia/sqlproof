from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlproof.client import InMemorySqlProofClient
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example


def stateful(
    proof: Any, *, sizes: dict[str, int], **kwargs: object
) -> Callable[[type[Any]], type[Any]]:
    del kwargs

    def decorate(cls: type[Any]) -> type[Any]:
        original_run = getattr(cls, "run", None)

        def run(self: Any) -> None:
            dataset = draw_example(dataset_strategy(proof.schema_info, sizes=sizes))
            db = InMemorySqlProofClient(dataset)
            if original_run is not None:
                original_run(self, db)

        cls.run = run
        return cls

    return decorate
