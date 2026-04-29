from __future__ import annotations

import json

from sqlproof.cli import main


def test_all_cli_subcommands_smoke(tmp_path, capsys) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text("CREATE TABLE events (id SERIAL PRIMARY KEY);", encoding="utf-8")
    types_file = tmp_path / "schema_types.py"
    counterexample = tmp_path / "counterexample.json"
    counterexample.write_text(json.dumps({"property_name": "example"}), encoding="utf-8")

    assert main(["version"]) == 0
    assert main(["introspect", "--schema-file", str(schema_file)]) == 0
    assert (
        main(["generate-types", "--schema-file", str(schema_file), "--output", str(types_file)])
        == 0
    )
    assert main(["replay", str(counterexample)]) == 0
    assert main(["clean-orphans"]) == 0

    output = capsys.readouterr().out
    assert "sqlproof" in output
    assert types_file.read_text(encoding="utf-8")
