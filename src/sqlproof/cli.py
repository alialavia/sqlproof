from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from sqlproof._version import __version__
from sqlproof.coverage.schema_shape import summarize_dataset_shape
from sqlproof.reporter.console import format_failure
from sqlproof.schema.model import Column, SchemaInfo, Table
from sqlproof.schema.parse_sql import parse_schema_sql


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlproof")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("version")

    introspect = subcommands.add_parser("introspect")
    introspect.add_argument("--schema-file", type=Path)
    introspect.add_argument("--dsn")
    introspect.add_argument("--format", choices=["json", "text"], default="text")

    generate_types_parser = subcommands.add_parser("generate-types")
    generate_types_parser.add_argument("--schema-file", type=Path, required=True)
    generate_types_parser.add_argument("--output", type=Path)
    generate_types_parser.add_argument(
        "--style", choices=["typeddict", "dataclass", "pydantic"], default="typeddict"
    )

    replay = subcommands.add_parser("replay")
    replay.add_argument("counterexample", type=Path)

    report = subcommands.add_parser("report")
    report.add_argument("counterexample", type=Path)
    report.add_argument("--format", choices=["json", "text"], default="text")

    run = subcommands.add_parser("run")
    run.add_argument("test_path")
    run.add_argument("pytest_options", nargs=argparse.REMAINDER)

    subcommands.add_parser("clean-orphans")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"sqlproof {__version__}")
        return 0
    if args.command == "introspect":
        if args.schema_file is None and args.dsn is None:
            parser.error("one of --schema-file or --dsn is required")
        if args.schema_file is not None:
            schema = parse_schema_sql(args.schema_file.read_text(encoding="utf-8"))
        else:
            from sqlproof import SqlProof

            schema = SqlProof.from_connection_string(str(args.dsn)).schema_info
        if args.format == "json":
            print(json.dumps(_schema_payload(schema), default=str))
        else:
            for table in schema.tables:
                print(f"{table.qualified_name} ({len(table.columns)} columns)")
        return 0
    if args.command == "generate-types":
        from sqlproof.types import generate_types

        output = generate_types(args.schema_file, style=args.style)
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output)
        return 0
    if args.command == "replay":
        payload = json.loads(args.counterexample.read_text(encoding="utf-8"))
        print(format_failure(payload))
        print(f"replay loaded {payload.get('property_name', 'counterexample')}")
        return 0
    if args.command == "report":
        payload = json.loads(args.counterexample.read_text(encoding="utf-8"))
        report_payload = _counterexample_report(payload)
        if args.format == "json":
            print(json.dumps(report_payload, default=str))
        else:
            print(format_failure(payload))
        return 0
    if args.command == "run":
        import pytest

        return int(pytest.main([args.test_path, *args.pytest_options]))
    if args.command == "clean-orphans":
        print("No orphaned SqlProof containers found.")
        return 0
    return 1


def _schema_payload(schema: SchemaInfo) -> dict[str, object]:
    return {"tables": [_table_payload(table) for table in schema.tables]}


def _table_payload(table: Table) -> dict[str, object]:
    return {
        "schema": table.schema,
        "name": table.name,
        "columns": [_column_payload(column) for column in table.columns],
        "primary_key": list(table.primary_key),
        "unique_constraints": [list(columns) for columns in table.unique_constraints],
        "partial_unique_constraints": [
            {"columns": list(pu.columns), "predicate": pu.predicate}
            for pu in table.partial_unique_constraints
        ],
        "exclusion_constraints": [
            {
                "columns_with_operators": [list(p) for p in exc.columns_with_operators],
                "access_method": exc.access_method,
            }
            for exc in table.exclusion_constraints
        ],
        "checks": [check.expression for check in table.check_constraints],
        "foreign_keys": [
            {
                "columns": list(foreign_key.columns),
                "referenced_table": foreign_key.referenced_table,
                "referenced_columns": list(foreign_key.referenced_columns),
                "on_delete": foreign_key.on_delete,
                "on_update": foreign_key.on_update,
            }
            for foreign_key in table.foreign_keys
        ],
    }


def _column_payload(column: Column) -> dict[str, object]:
    return {
        "name": column.name,
        "type": column.type.name,
        "nullable": column.nullable,
        "default": column.default,
        "is_generated": column.is_generated,
        "identity": column.identity,
    }


def _counterexample_report(payload: dict[str, object]) -> dict[str, object]:
    dataset = payload.get("dataset")
    shape = (
        summarize_dataset_shape(cast(dict[str, list[dict[str, Any]]], dataset))
        if isinstance(dataset, dict)
        else {}
    )
    return {
        "property_name": payload.get("property_name", "counterexample"),
        "schema_fingerprint": payload.get("schema_fingerprint"),
        "row_context": payload.get("row_context", {}),
        "failure": payload.get("failure", {}),
        "shape": shape,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
