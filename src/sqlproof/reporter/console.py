from __future__ import annotations

from typing import Any, cast

from sqlproof.coverage.schema_shape import summarize_dataset_shape


def format_failure(payload: dict[str, Any]) -> str:
    lines = [
        f"Property failed: {payload.get('property_name', 'counterexample')}",
        f"Failure: {payload.get('failure', {}).get('kind', 'unknown')}: "
        f"{payload.get('failure', {}).get('message', '')}",
    ]
    if payload.get("row_context"):
        lines.append(f"Row context: {payload['row_context']}")
    dataset = payload.get("dataset")
    if isinstance(dataset, dict):
        shape = summarize_dataset_shape(cast(dict[str, list[dict[str, Any]]], dataset))
        lines.append(f"Dataset shape: {shape}")
    return "\n".join(lines)
