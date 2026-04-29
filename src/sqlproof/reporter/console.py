from __future__ import annotations

from typing import Any


def format_failure(payload: dict[str, Any]) -> str:
    return f"Property failed: {payload['property_name']}"
