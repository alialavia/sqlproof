from __future__ import annotations

from pathlib import Path

from sqlproof.schema.parse_sql import parse_schema_sql


def generate_types(schema_file: Path, *, style: str = "typeddict") -> str:
    schema = parse_schema_sql(schema_file.read_text(encoding="utf-8"))
    lines = ["from __future__ import annotations"]
    if style == "typeddict":
        lines.append("from typing import TypedDict")
        for table in schema.tables:
            class_name = _class_name(table.name)
            lines.extend(["", f"class {class_name}(TypedDict):"])
            for column in table.columns:
                lines.append(f"    {column.name}: object")
    elif style == "dataclass":
        lines.append("from dataclasses import dataclass")
        for table in schema.tables:
            lines.extend(["", "@dataclass", f"class {_class_name(table.name)}:"])
            for column in table.columns:
                lines.append(f"    {column.name}: object")
    else:
        lines.append("from pydantic import BaseModel")
        for table in schema.tables:
            lines.extend(["", f"class {_class_name(table.name)}(BaseModel):"])
            for column in table.columns:
                lines.append(f"    {column.name}: object")
    return "\n".join(lines) + "\n"


def _class_name(table_name: str) -> str:
    return "".join(part.capitalize() for part in table_name.split("_")).removesuffix("s")
