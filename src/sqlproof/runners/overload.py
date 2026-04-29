from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlproof.client import InMemorySqlProofClient
from sqlproof.generators.functions import FunctionCall


def function_overloads(
    proof: Any,
    *,
    function: str,
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del proof, kwargs

    def decorate(callback: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            call_a = FunctionCall(sql=f"{function}()", overload_name=f"{function}/0")
            call_b = FunctionCall(sql=f"{function}()", overload_name=f"{function}/0")
            callback(InMemorySqlProofClient({}), call_a, call_b)

        return wrapped

    return decorate
