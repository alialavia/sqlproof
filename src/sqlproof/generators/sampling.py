from __future__ import annotations

import warnings
from typing import TypeVar

from hypothesis.errors import NonInteractiveExampleWarning
from hypothesis.strategies import SearchStrategy

T = TypeVar("T")


def draw_example(strategy: SearchStrategy[T]) -> T:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NonInteractiveExampleWarning)
        return strategy.example()
