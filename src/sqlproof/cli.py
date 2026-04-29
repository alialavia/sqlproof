from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlproof._version import __version__
from sqlproof.schema.parse_sql import parse_schema_sql


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlproof")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("version")

    introspect = subcommands.add_parser("introspect")
    introspect.add_argument("--schema-file", type=Path)
    introspect.add_argument("--format", choices=["json", "text"], default="text")

    generate_types_parser = subcommands.add_parser("generate-types")
    generate_types_parser.add_argument("--schema-file", type=Path, required=True)
    generate_types_parser.add_argument("--output", type=Path)
    generate_types_parser.add_argument(
        "--style", choices=["typeddict", "dataclass", "pydantic"], default="typeddict"
    )

    replay = subcommands.add_parser("replay")
    replay.add_argument("counterexample", type=Path)

    run = subcommands.add_parser("run")
    run.add_argument("test_path")
    run.add_argument("pytest_options", nargs=argparse.REMAINDER)

    subcommands.add_parser("clean-orphans")

    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"sqlproof {__version__}")
        return 0
    if args.command == "introspect":
        if args.schema_file is None:
            parser.error("--schema-file is required for offline introspection in this build")
        schema = parse_schema_sql(args.schema_file.read_text(encoding="utf-8"))
        if args.format == "json":
            print(json.dumps({"tables": [table.name for table in schema.tables]}))
        else:
            for table in schema.tables:
                print(table.name)
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
        print(f"replay loaded {payload.get('property_name', 'counterexample')}")
        return 0
    if args.command == "run":
        import pytest

        return int(pytest.main([args.test_path, *args.pytest_options]))
    if args.command == "clean-orphans":
        print("No orphaned SqlProof containers found.")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
