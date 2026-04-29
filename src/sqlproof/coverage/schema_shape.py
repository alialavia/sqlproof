from __future__ import annotations

from typing import Any


def summarize_dataset_shape(dataset: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, int]]:
    return {table: {"rows": len(rows)} for table, rows in dataset.items()}
