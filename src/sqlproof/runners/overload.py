from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlproof.generators.functions import FunctionCall


def function_overloads(
    proof: Any,
    *,
    function: str,
    **kwargs: object,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    del kwargs

    def decorate(callback: Callable[..., None]) -> Callable[..., None]:
        def wrapped() -> None:
            calls = _function_calls(proof, function)
            if len(calls) < 2:
                calls = (
                    FunctionCall(sql=f"{function}()", overload_name=f"{function}/0"),
                    FunctionCall(sql=f"{function}()", overload_name=f"{function}/0"),
                )
            with proof.client_for_dataset({}) as db:
                callback(db, calls[0], calls[1])

        return wrapped

    return decorate


def _function_calls(proof: Any, function_name: str) -> tuple[FunctionCall, ...]:
    calls: list[FunctionCall] = []
    for function in proof.schema_info.functions:
        if function.name != function_name:
            continue
        arguments = ", ".join("%s" for _arg in function.arg_types)
        overload = f"{function.name}/{len(function.arg_types)}"
        calls.append(FunctionCall(sql=f"{function.name}({arguments})", overload_name=overload))
    return tuple(calls)
