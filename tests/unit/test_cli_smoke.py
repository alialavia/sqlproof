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


def test_cli_outputs_structured_introspection_and_counterexample_report(tmp_path, capsys) -> None:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(
        """
        CREATE TABLE events (
          id SERIAL PRIMARY KEY,
          name TEXT NOT NULL CHECK (length(name) <= 12)
        );
        """,
        encoding="utf-8",
    )
    counterexample = tmp_path / "counterexample.json"
    counterexample.write_text(
        json.dumps(
            {
                "property_name": "event_name",
                "schema_fingerprint": "sha256:abc",
                "row_context": {"event_id": 1},
                "dataset": {"events": [{"id": 1, "name": ""}]},
                "failure": {"kind": "AssertionError", "message": "empty"},
            }
        ),
        encoding="utf-8",
    )

    assert main(["introspect", "--schema-file", str(schema_file), "--format", "json"]) == 0
    introspection = json.loads(capsys.readouterr().out)
    assert introspection["tables"][0]["columns"][1]["name"] == "name"
    assert introspection["tables"][0]["checks"] == ["length(name) <= 12"]

    assert main(["report", str(counterexample), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["property_name"] == "event_name"
    assert report["shape"] == {"events": {"rows": 1}}

    assert main(["report", str(counterexample)]) == 0
    assert "event_name" in capsys.readouterr().out
