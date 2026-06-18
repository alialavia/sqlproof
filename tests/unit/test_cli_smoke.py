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


def test_mutation_report_on_empty_dir_writes_no_runs_page(tmp_path) -> None:
    output = tmp_path / "report.html"
    runs_dir = tmp_path / "runs"
    assert main(["mutation", "report", "--runs-dir", str(runs_dir), "--output", str(output)]) == 0
    html = output.read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "no runs found" in html.lower()


def test_mutation_report_renders_existing_runs(tmp_path) -> None:
    import json

    from sqlproof.mutation.artifact import RunArtifact
    from sqlproof.mutation.result import MutantOutcome

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    artifact = RunArtifact(
        run_id="aaaaaaaa",
        started_at="2026-06-11T10:00:00Z",
        duration_s=5.0,
        sqlproof_version="0.9.0",
        git_sha="abc1234",
        git_dirty=False,
        hypothesis_seed=42,
        schema_fingerprint="sha256:s1",
        pytest_args=("tests/",),
        outcomes=(
            MutantOutcome(
                mutant_id="s1",
                target="billing.f",
                description="drop FILTER",
                status="survived",
                pytest_exit_code=0,
                hypothesis_seed=42,
                detail=None,
                duration_s=0.5,
            ),
        ),
    )
    (runs_dir / "run.json").write_text(json.dumps(artifact.to_json_dict()), encoding="utf-8")

    output = tmp_path / "report.html"
    assert main(["mutation", "report", "--runs-dir", str(runs_dir), "--output", str(output)]) == 0
    html = output.read_text(encoding="utf-8")
    assert "billing.f" in html
    assert "--hypothesis-seed=42" in html
