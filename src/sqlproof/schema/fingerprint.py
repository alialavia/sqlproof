from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from sqlproof.schema.model import SchemaInfo


def _canonical(value: Any) -> Any:
    if is_dataclass(value):
        return _canonical(asdict(cast(Any, value)))
    if isinstance(value, dict):
        items = cast("dict[str, Any]", value)
        return {key: _canonical(items[key]) for key in sorted(items)}
    if isinstance(value, tuple):
        tuple_value = cast("tuple[Any, ...]", value)  # type: ignore[redundant-cast]
        return [_canonical(item) for item in tuple_value]
    if isinstance(value, list):
        list_value = cast("list[Any]", value)  # type: ignore[redundant-cast]
        return [_canonical(item) for item in list_value]
    return value


def compute(schema_info: SchemaInfo) -> str:
    canonical_json = json.dumps(
        _canonical(schema_info),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
