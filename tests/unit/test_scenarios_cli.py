"""`sqlproof scenarios` and scenario discovery, without a database."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from sqlproof.cli import main
from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scenarios.discover import discover, find

SCHEMA_SQL = """
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  created_at timestamptz NOT NULL DEFAULT now(),
  token text DEFAULT md5(clock_timestamp()::text)
);
"""


def _write(directory: Path, filename: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _write(
        tmp_path / "scenarios",
        "billing_scenarios.py",
        """
        from helpers import TIERS
        from sqlproof.scenarios import scenario

        @scenario(seed=5)
        def tiered(s):
            first = s.row("customers", name="first", tier=TIERS[0])
            s.table("customers").cohort(2, tier=2).fill(3)

        not_a_scenario = 42
        """,
    )
    _write(tmp_path / "scenarios", "helpers.py", "TIERS = (1, 2, 3)\n")
    _write(
        tmp_path / "scenarios" / "nested",
        "empty_scenarios.py",
        """
        from sqlproof.scenarios import scenario

        @scenario(seed=1, name="blank")
        def anything(s):
            s.table("customers").fill(1)
        """,
    )
    (tmp_path / "schema.sql").write_text(SCHEMA_SQL, encoding="utf-8")
    return tmp_path


def test_discover_collects_every_scenario_sorted_by_name(project: Path) -> None:
    found = discover(project / "scenarios")
    assert [s.name for s in found] == ["blank", "tiered"]
    assert found[1].source is not None
    assert found[1].source.endswith("billing_scenarios.py")
    assert find("tiered", project / "scenarios").seed == 5


def test_discover_rejects_duplicate_names(project: Path) -> None:
    _write(
        project / "scenarios",
        "again_scenarios.py",
        """
        from sqlproof.scenarios import scenario

        @scenario(seed=2)
        def tiered(s):
            pass
        """,
    )
    with pytest.raises(SqlProofScenarioError, match="defined twice"):
        discover(project / "scenarios")


def test_missing_directory_and_unknown_name(project: Path) -> None:
    with pytest.raises(SqlProofScenarioError, match="No scenarios directory"):
        discover(project / "nowhere")
    with pytest.raises(SqlProofScenarioError, match="Known: blank, tiered"):
        find("ghost", project / "scenarios")


def test_cli_list(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scenarios", "list", "--dir", str(project / "scenarios")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[:2] for line in lines] == [
        ["blank", "seed=1"],
        ["tiered", "seed=5"],
    ]


def test_cli_show_text(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["scenarios", "show", "tiered", "--dir", str(project / "scenarios")]
    assert main([*args, "--schema-file", str(project / "schema.sql")]) == 0
    out = capsys.readouterr().out
    assert "tiered (seed 5, clock 2026-01-01T00:00:00+00:00)" in out
    assert "public.customers: 6 rows" in out
    assert "ref first = 1" in out
    assert "warning: public.customers.token" in out


def test_cli_show_json(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["scenarios", "show", "tiered", "--dir", str(project / "scenarios")]
    assert main([*args, "--schema-file", str(project / "schema.sql"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == {"public.customers": 6}
    assert payload["refs"] == {"first": 1}
    assert len(payload["fingerprint"]) == 64


def test_cli_reports_scenario_errors(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["scenarios", "show", "ghost", "--dir", str(project / "scenarios")]
    assert main([*args, "--schema-file", str(project / "schema.sql")]) == 2
    assert "error: No scenario named 'ghost'" in capsys.readouterr().err
