from __future__ import annotations

import json
from dataclasses import is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.types.range import Range


def write_counterexample(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, default=_json_default, indent=2, sort_keys=True), encoding="utf-8"
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal | datetime | date | time | UUID):
        return str(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Range):
        # Range values come from pgvector/range columns. Serialize
        # structurally so counterexample files can round-trip cleanly
        # (lower/upper get the standard Decimal/datetime/etc.
        # treatment recursively via json.dumps).
        return {
            "__type__": "Range",
            "lower": value.lower,
            "upper": value.upper,
            "bounds": value.bounds,
        }
    if is_dataclass(value):
        return value.__dict__
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
